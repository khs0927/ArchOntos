"""Offline contract checks; does not access Drive or establish parser correctness."""
import copy,json,sqlite3
from pathlib import Path
from jsonschema import Draft202012Validator
P=Path(__file__).parent
schema=json.loads((P/'extraction-result.schema.json').read_text())
Draft202012Validator.check_schema(schema)
validator=Draft202012Validator(schema)
def validate_result(data):
    validator.validate(data)
    ids=[e['evidence_id'] for e in data['evidence']]
    if len(ids)!=len(set(ids)): raise ValueError('duplicate evidence ID')
    if any(a['evidence_id'] not in ids for a in data['assertions']): raise ValueError('missing evidence reference')
    c=data['coverage']
    if c['expected'] is not None and c['processed']>c['expected']: raise ValueError('invalid coverage')
    if data['status']=='COMPLETE' and c['processed']!=c['expected']: raise ValueError('incomplete scope')

def main():
    checks=[]
    def reject(name,fn,exception):
        try: fn()
        except exception: checks.append(name); return
        raise AssertionError('not rejected: '+name)
    db=sqlite3.connect(':memory:')
    db.executescript((P/'catalog.sql').read_text())
    assert db.execute('PRAGMA foreign_keys').fetchone()[0]==1
    db.execute("INSERT INTO sources VALUES ('s','LOCAL_FIXTURE','test')")
    db.execute("INSERT INTO corpora VALUES ('c','s','user')")
    db.execute("INSERT INTO artifacts(artifact_id,source_id,external_id,kind,name,observed_at) VALUES ('a','s','file1','FILE','plan.dxf','2026-09-24')")
    db.execute("INSERT INTO snapshots VALUES ('v','a','rev1','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','original','CAPTURED','fixture.bin',1,'2026-09-24')")
    db.execute("INSERT INTO jobs(job_id,snapshot_id,stage,profile_id,profile_version,parser_id,parser_version,config_hash) VALUES ('j','v','text','full-text','1','fixture','1','config1')")
    reject('foreign key rejects nonexistent snapshot',lambda:db.execute("INSERT INTO jobs(job_id,snapshot_id,stage,profile_id,profile_version,parser_id,parser_version,config_hash) VALUES ('bad','missing','text','full-text','1','fixture','1','c')"),sqlite3.IntegrityError)
    reject('duplicate logical run rejected',lambda:db.execute("INSERT INTO jobs(job_id,snapshot_id,stage,profile_id,profile_version,parser_id,parser_version,config_hash) VALUES ('j2','v','text','full-text','1','fixture','1','config1')"),sqlite3.IntegrityError)
    reject('assertion without evidence rejected',lambda:db.execute("INSERT INTO assertions(assertion_id,job_id,evidence_id,subject_uri,predicate_uri,object_json) VALUES ('x','j','missing','a','b','1')"),sqlite3.IntegrityError)
    db.execute("INSERT INTO snapshots VALUES ('v2','a','rev2','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','original','CAPTURED','fixture.bin',1,'2026-09-24')")
    db.execute("INSERT INTO jobs(job_id,snapshot_id,stage,profile_id,profile_version,parser_id,parser_version,config_hash) VALUES ('other','v2','text','full-text','1','fixture','1','config1')")
    reject('evidence with wrong snapshot rejected',lambda:db.execute("INSERT INTO evidence VALUES ('bad','j','v2','{}',0.8)"),sqlite3.IntegrityError)
    db.execute("INSERT INTO evidence VALUES ('e2','other','v2','{}',0.8)")
    reject('assertion with different run evidence rejected',lambda:db.execute("INSERT INTO assertions(assertion_id,job_id,evidence_id,subject_uri,predicate_uri,object_json) VALUES ('bad','j','e2','a','b','1')"),sqlite3.IntegrityError)
    db.execute("INSERT INTO evidence VALUES ('e','j','v','{\"page\":1}',0.9)")
    db.execute("INSERT INTO assertions(assertion_id,job_id,evidence_id,subject_uri,predicate_uri,object_json) VALUES ('x','j','e','a','b','1')")
    reject('acceptance without reviewer rejected',lambda:db.execute("UPDATE assertions SET review_state='ACCEPTED' WHERE assertion_id='x'"),sqlite3.IntegrityError)
    reject('owl sameAs rejected',lambda:db.execute("UPDATE assertions SET predicate_uri='http://www.w3.org/2002/07/owl#sameAs' WHERE assertion_id='x'"),sqlite3.IntegrityError)
    db.execute("UPDATE assertions SET review_state='STALE' WHERE assertion_id='x'")
    checks.append('stale assertions retain provenance')
    reject('captured snapshot without hash rejected',lambda:db.execute("INSERT INTO snapshots VALUES ('bad','a','bad',NULL,'original','CAPTURED','fixture',1,'now')"),sqlite3.IntegrityError)
    reject('metadata is not a content snapshot',lambda:db.execute("INSERT INTO snapshots SELECT 'bad',artifact_id,'bad',sha256,representation,'METADATA_ONLY',bytes_uri,byte_size,captured_at FROM snapshots WHERE snapshot_id='v'"),sqlite3.IntegrityError)
    db.execute("INSERT INTO metadata_observations VALUES ('o','a','now','INACCESSIBLE','{}')")
    checks.append('inaccessible metadata observation retained separately')
    db.execute("INSERT INTO corpora VALUES ('c2','s','shared-drive')")
    db.execute("INSERT INTO artifact_corpus_observations VALUES ('a','c','s','t1')")
    db.execute("INSERT INTO artifact_corpus_observations VALUES ('a','c2','s','t2')")
    reject('corpus move cannot duplicate source file',lambda:db.execute("INSERT INTO artifacts(artifact_id,source_id,external_id,kind,name,observed_at) VALUES ('duplicate','s','file1','FILE','renamed','now')"),sqlite3.IntegrityError)
    db.execute("INSERT INTO jobs(job_id,snapshot_id,stage,profile_id,profile_version,parser_id,parser_version,config_hash) VALUES ('profile2','v','text','full-text','2','fixture','1','config1')")
    checks.append('new profile version creates distinct job')
    for i in range(2):
        db.execute("INSERT INTO artifacts(artifact_id,source_id,external_id,kind,name,observed_at) VALUES (?,?,?,'ARCHIVE_MEMBER','plan.dxf','now')",(f'm{i}','s',f'v:{i}:plan.dxf'))
        db.execute("INSERT INTO archive_members VALUES (?,'v',?,'./plan.dxf','plan.dxf')",(f'm{i}',i))
    checks.append('duplicate archive paths preserve distinct ordinals')
    reject('duplicate archive ordinal rejected',lambda:db.execute("UPDATE archive_members SET member_ordinal=0 WHERE artifact_id='m1'"),sqlite3.IntegrityError)
    db.execute("INSERT INTO change_cursors VALUES ('c','p0','now')")
    db.commit()
    db.execute('BEGIN')
    db.execute("INSERT INTO change_page_receipts VALUES ('c','p0','p1','now')")
    db.execute("UPDATE change_cursors SET committed_token='p1' WHERE corpus_id='c' AND committed_token='p0'")
    db.rollback()
    assert db.execute('SELECT committed_token FROM change_cursors').fetchone()[0]=='p0'
    assert db.execute('SELECT count(*) FROM change_page_receipts').fetchone()[0]==0
    checks.append('rollback preserves cursor and receipt atomicity')
    data={'contract_version':'4.0','job_id':'j','snapshot_id':'v','status':'COMPLETE','extractor':{'name':'fixture','version':'1','config_hash':'c'},'coverage':{'scope':'TEXT','profile_id':'full-text','profile_version':'1','expected':1,'processed':1,'unknown_remainder':False},'evidence':[{'evidence_id':'e','locator':{'page':1}}],'assertions':[{'subject':'x','predicate':'p','object':'v','evidence_id':'e','review_state':'PROVISIONAL'}],'issues':[]}
    validate_result(data);checks.append('valid complete scoped extraction accepted')
    for name,mutate in [
        ('profile version required',lambda x:x['coverage'].pop('profile_version')),
        ('owl sameAs extraction rejected',lambda x:x['assertions'][0].update(predicate='owl:sameAs')),
        ('unknown remainder cannot be complete',lambda x:x['coverage'].update(unknown_remainder=True)),
        ('incomplete count cannot be complete',lambda x:x['coverage'].update(processed=0)),
        ('missing evidence reference rejected',lambda x:x['assertions'][0].update(evidence_id='missing')),
        ('extractor cannot self-approve assertions',lambda x:x['assertions'][0].update(review_state='ACCEPTED')),
        ('unsupported cannot emit assertions',lambda x:x.update(status='UNSUPPORTED',issues=['no parser']))]:
        bad=copy.deepcopy(data);mutate(bad)
        reject(name,lambda:validate_result(bad),Exception)
    (P/'validation-result.json').write_text(json.dumps({'status':'PASS','checks':checks,'limits':'Offline synthetic contract validation only; no Drive scan, file parser or production runtime validated.'},indent=2)+'\n')
    print(json.dumps({'status':'PASS','checks':len(checks)}))
if __name__=='__main__':main()
