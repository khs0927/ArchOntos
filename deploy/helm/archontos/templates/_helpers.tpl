{{/*
Effective autoscaling settings for one service: services.<name>.autoscaling overrides the
global .Values.autoscaling key by key. `enabled` is resolved with hasKey because Sprig's
merge treats an explicit `false` as empty and would let the global value win.
Usage: include "archontos.autoscaling" (dict "svc" $svc "root" $) | fromYaml
*/}}
{{- define "archontos.autoscaling" -}}
{{- $svcHpa := default dict .svc.autoscaling -}}
{{- $out := merge (deepCopy $svcHpa) (deepCopy .root.Values.autoscaling) -}}
{{- if hasKey $svcHpa "enabled" -}}
{{- $_ := set $out "enabled" $svcHpa.enabled -}}
{{- end -}}
{{- toYaml $out -}}
{{- end -}}
