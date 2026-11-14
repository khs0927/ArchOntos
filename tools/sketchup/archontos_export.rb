# frozen_string_literal: true
#
# ArchOntos SketchUp exporter: writes the `archontos-skp-export/1` JSON contract
# (docs/MVP4-IFC-SKETCHUP.md, schema `archontos.cad.skp.SKP_EXPORT_SCHEMA`).
#
# Usage (SketchUp 2021+, Ruby console or a SketchUp MCP `execute_ruby` / `eval_ruby` call):
#
#   load 'C:/path/to/ArchOntos/tools/sketchup/archontos_export.rb'
#   ArchOntos::SkpExport.export('C:/temp/model.archontos.json')
#
# then upload the file:  POST /v1/cad/artifacts?project_id=..&source_key=..&revision_label=..
# (content-type application/json, or source_format=skp_json).
#
# What it writes (no SketchUp state is changed; read-only walk):
#   * model.units   "mm" (lengths are converted from SketchUp's internal inches)
#   * storeys       from the model attribute dictionary "archontos" key "storeys"
#                   ([{"name": "1F", "elevation": 0}] in mm, JSON string), else omitted
#   * entities      every Group / ComponentInstance (nested ones flattened with their world
#                   transformation): persistent_id, entity_type, tag, name, definition,
#                   ifc_type / ifc_guid (IFC classification, if any), storey (entity attribute
#                   "archontos"/"storey"), world bbox, transform, footprint (lowest horizontal
#                   face of a group, if it has one).
# Entities nested inside a classified (ifc_type) or door/window component are not descended
# into: the outer element is the building element.
#
# Kept intentionally small and dependency-free; it cannot run in CI. The output shape is
# checked by tests/test_skp_exporter_contract.py against tools/sketchup/sample_export.json.

require 'json'

module ArchOntos
  module SkpExport
    SCHEMA = 'archontos-skp-export/1'
    EXPORTER = 'archontos_export.rb 1.0'
    MM_PER_INCH = 25.4
    IFC_SCHEMAS = ['IFC 4', 'IFC 2x3'].freeze
    GUID_RE = /\A[0-9A-Za-z_$]{22}\z/.freeze
    IFC_TYPE_RE = /\AIfc[A-Za-z]{2,60}\z/.freeze
    STOP_DESCENT_RE = /\A(door|window|문|창)/i.freeze

    module_function

    def mm(length)
      (length.to_f * MM_PER_INCH).round(3)
    end

    def point_mm(pt)
      [mm(pt.x), mm(pt.y), mm(pt.z)]
    end

    def ifc_type(definition)
      IFC_SCHEMAS.each do |schema|
        value = definition.get_attribute('AppliedSchemaTypes', schema)
        return value if value.is_a?(String) && value.match?(IFC_TYPE_RE)
      end
      nil
    end

    # Classified components keep their IFC GlobalId in a nested attribute dictionary:
    # definition -> "IFC 4"|"IFC 2x3" -> "GlobalId" -> "value".
    def ifc_guid(instance)
      [instance, instance.respond_to?(:definition) ? instance.definition : nil].compact.each do |e|
        IFC_SCHEMAS.each do |schema|
          dict = e.attribute_dictionary(schema)
          next unless dict

          gid = dict.attribute_dictionary('GlobalId')
          value = gid && gid['value']
          return value if value.is_a?(String) && value.match?(GUID_RE)
        end
      end
      nil
    end

    def world_bbox(entity, parent_tr)
      bb = Geom::BoundingBox.new
      local = entity.definition.bounds
      8.times { |i| bb.add(local.corner(i).transform(parent_tr * entity.transformation)) }
      { 'min' => point_mm(bb.min), 'max' => point_mm(bb.max) }
    end

    def footprint(entity, world_tr)
      faces = entity.definition.entities.grep(Sketchup::Face).select do |f|
        f.normal.parallel?(Z_AXIS)
      end
      return nil if faces.empty?

      face = faces.min_by { |f| f.vertices.map { |v| v.position.z }.min }
      pts = face.outer_loop.vertices.map { |v| v.position.transform(world_tr) }
      return nil if pts.length < 3

      pts.map { |p| [mm(p.x), mm(p.y)] }
    end

    def entity_json(entity, parent_tr)
      world_tr = parent_tr * entity.transformation
      definition = entity.definition
      out = {
        'persistent_id' => entity.persistent_id,
        'entity_type' => entity.is_a?(Sketchup::Group) ? 'Group' : 'ComponentInstance',
        'tag' => (entity.layer ? entity.layer.name : 'Untagged'),
        'bbox' => world_bbox(entity, parent_tr),
        'transform' => world_tr.to_a.each_with_index.map { |v, i| i >= 12 && i < 15 ? mm(v) : v.to_f.round(9) }
      }
      out['name'] = entity.name unless entity.name.to_s.empty?
      out['definition'] = definition.name if entity.is_a?(Sketchup::ComponentInstance)
      type = ifc_type(definition)
      out['ifc_type'] = type if type
      guid = ifc_guid(entity)
      out['ifc_guid'] = guid if guid
      storey = entity.get_attribute('archontos', 'storey')
      out['storey'] = storey.to_s if storey && !storey.to_s.empty?
      if entity.is_a?(Sketchup::Group)
        fp = footprint(entity, world_tr)
        out['footprint'] = fp if fp
      end
      out
    end

    def walk(entities, parent_tr, out)
      entities.each do |e|
        next unless e.is_a?(Sketchup::Group) || e.is_a?(Sketchup::ComponentInstance)
        next if e.hidden?

        json = entity_json(e, parent_tr)
        out << json
        stop = json['ifc_type'] || json['definition'].to_s.match?(STOP_DESCENT_RE)
        walk(e.definition.entities, parent_tr * e.transformation, out) unless stop
      end
      out
    end

    def storeys(model)
      raw = model.get_attribute('archontos', 'storeys')
      return nil if raw.nil? || raw.to_s.empty?

      list = JSON.parse(raw.to_s)
      list.map { |s| { 'name' => s.fetch('name').to_s, 'elevation' => s.fetch('elevation').to_f } }
    rescue JSON::ParserError, KeyError, TypeError
      nil
    end

    def document(model = Sketchup.active_model)
      doc = {
        'schema' => SCHEMA,
        'model' => {
          'name' => model.title.to_s,
          'guid' => model.guid.to_s,
          'units' => 'mm',
          'exporter' => EXPORTER,
          'sketchup_version' => Sketchup.version.to_s
        },
        'entities' => walk(model.entities, Geom::Transformation.new, [])
      }
      list = storeys(model)
      doc['storeys'] = list if list && !list.empty?
      doc
    end

    def export(path, model = Sketchup.active_model)
      File.write(path, JSON.pretty_generate(document(model)), mode: 'w:UTF-8')
      path
    end
  end
end
