"""Small deterministic checks; no paid model calls or user project changes."""
import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from pypdf import PdfWriter
from v_5.main import create_app
from v_5.ingest.codebook_ingestion.sub_agents.agent import PdfCodebookReply
from v_5.ingest.codebook_ingestion.tables import create_master, normalize_rows, join_codebooks, COLUMNS
from v_5.ingest.codebook_ingestion.sub_agents.pdf import pdf_codebook
from v_5.ingest.codebook_ingestion.sessions import agent_turn
from v_5.ingest.codebook_ingestion.functions import ingest_codebooks
from v_5.ingest.manifest import validate_prepared_manifest


def row(name, description='Definition', location='Page 1'):
    """Make one predictable source-codebook row for assertions.

    Inputs: name: variable; description/location: optional evidence text.
    Returns: Dict with the normalized source-codebook fields.
    Details: Test fixture used by extraction and merge response stubs.
    """
    return dict(variable_name=name, variable_label=name, variable_description=description,
                other_information='not in source', values='character',
                source_location=location, source_presence='in source')


def run():
    """Check codebook assembly, PDF workers and publication inputs offline.

    Inputs: No arguments; creates temporary files and simulated responses.
    Returns: None; prints success or raises an assertion.
    Details: Exercises roster/join behavior, page boundaries, reconciliation batches,
        codebook budgets and the PDF upload limit without model calls.
    """
    with tempfile.TemporaryDirectory() as temporary:
        root=Path(temporary)
        pd.DataFrame({'ID':['01','02'],'Age':[20,None]}).to_parquet(root/'part1.parquet')
        pd.DataFrame({'ID':['01','02'],'Score':[None,None]}).to_parquet(root/'part2.parquet')
        canonical = {'tables': [{'name': 'part1', 'path': '/work/part1.parquet', 'row_count': 2,
                                'primary_key': ['ID'], 'columns': [
                                    {'name': 'ID', 'original_name': 'ID', 'type': 'character', 'sql_definition': 'TEXT'},
                                    {'name': 'Age', 'original_name': 'Age', 'type': 'int', 'sql_definition': 'BIGINT'},
                                ]}]}
        (root/'canonical.json').write_text(json.dumps(canonical))
        validate_prepared_manifest(root, '/work/canonical.json')
        column = canonical['tables'][0]['columns'][1]
        column['final_name'] = column.pop('name')
        (root/'canonical.json').write_text(json.dumps(canonical))
        try:
            validate_prepared_manifest(root, '/work/canonical.json')
        except ValueError as error:
            assert 'tables.0.columns.1.name' in str(error)
        else:
            raise AssertionError('A new manifest without the required column name was accepted.')
        manifest={'tables':[
            {'name':'part1','file_path':'/work/part1.parquet','columns':{'ID':'character','Age':'int'}},
            {'name':'part2','file_path':'/work/part2.parquet','columns':[
                {'final_name':'ID','type':'character'},
                {'name':'SourceScore','original_name':'Score','final_name':'Score','type':'numeric'},
            ]},
        ]}
        (root/'manifest.json').write_text(json.dumps(manifest))
        master=create_master(root,{'manifest_path':'/work/manifest.json'})
        assert list(master.variable_name)==['id','age','score']
        assert list(master.columns[:2])==['variable_name','% missing']
        assert list(master['% missing'])==[0,50,100]
        assert list(master['variable_type'])==['character','int','numeric']
        assert master.iloc[0].table_names=='part1\npart2'
        source=normalize_rows([row('ID'),row('EXTRA')])
        combined,unmatched=join_codebooks(master,[('abc','abc.csv',source)])
        assert len(combined)==3 and list(unmatched.variable_name)==['extra']
        assert list(combined['% missing'])==[0,50,100]
        assert list(combined['variable_type'])==['character','int','numeric']
        assert combined.loc[combined.variable_name=='age','source_presence_abc'].iloc[0]=='not in source'
        assert combined.loc[combined.variable_name=='id','source_presence_abc'].iloc[0]=='in source'
        empty_master = master.assign(**{'% missing': None})
        empty_combined, _ = join_codebooks(empty_master, [('abc','abc.csv',source)])
        assert empty_combined['% missing'].isna().all()
        try:
            normalize_rows([row('ID'),row('id')])
        except ValueError: pass
        else: raise AssertionError('Case collision was silently accepted')

        class Session:
            def __init__(self):
                """Initialize the codebook test's minimal session recorder.

                Inputs: No arguments; uses empty test state.
                Returns: None; creates calls, state and messages collections.
                Details: Test-only replacement for the parts of Session used by codebook helpers.
                """
                self.calls=[]; self.state={}; self.messages=[]
            def check_stop(self):
                """Skip the real session side effect in this offline check.

                Inputs: No external inputs.
                Returns: None.
                Details: Test-only Session stub; cancellation/persistent journals are outside this
                    codebook check.
                """
                pass
            def record(self,*args):
                """Skip the real session side effect in this offline check.

                Inputs: args: ignored event arguments.
                Returns: None.
                Details: Test-only Session stub; cancellation/persistent journals are outside this
                    codebook check.
                """
                pass
            def message(self,*args):
                """Capture a codebook progress message for the test.

                Inputs: args: positional message arguments.
                Returns: None; appends the arguments to messages.
                Details: Test double used instead of persisting UI messages.
                """
                self.messages.append(args)
            def count_input(self,request,agent=None):
                """Return a fixed input cost for codebook budget tests.

                Inputs: request: ignored model payload; agent: optional ignored label.
                Returns: 100 tokens.
                Details: Test-only count keeps budget behavior deterministic without an API call.
                """
                return 100
            def call_loader(self,name,request,limit):
                """Supply an empty completed PDF extraction response.

                Inputs: name/request/limit: model-call arguments; records request.
                Returns: (text, usage, status): empty PDF JSON reply, fixed token counts and completed.
                Details: Test double used to exercise agent_turn without a live model.
                """
                self.calls.append(request)
                return json.dumps({'rows':[],'general_information':[],'unfinished_entry':'','issues':[]}),{'input_tokens':100,'output_tokens':20},'completed'
        session=Session()
        files={f'file_{n}':dict(file_id=f'file_{n}',filename=name,path='unused',format='csv',asset_kind='codebook')
               for n,name in enumerate(['abc_one.csv','abc_two.csv'])}
        for file in files.values():
            file['path'] = str(root / file['filename'])
            source.to_csv(file['path'], index=False)  # Current ingestion fingerprints the uploaded bytes.
        with patch('v_5.ingest.codebook_ingestion.functions.structured_codebook',return_value=source):
            outputs=ingest_codebooks(session,{'folder':'Example','files':files},{'manifest_path':'/work/manifest.json'},files,root,'test packages')
        assert list(outputs['source_tables'])==['abc','abc2']
        assert Path(outputs['combined']).exists() and Path(outputs['unmatched']).exists()
        assert pd.read_csv(outputs['combined']).shape[0]==3
        assert Path(outputs['source_tables']['abc']['table']).name=='abc_one_codebook.csv'
        pages={'page_count':12,'pages':[{'page':n,'markdown':f'Page {n}'} for n in range(1,13)]}
        calls, reconciliation_sizes = [], []
        related = [f'var_{n}' for n in range(31)]

        def pdf_reply(session,name,prompt,history,payload,contract,counter,model=None):
            """Simulate PDF extraction, within-block merges and block reconciliation.

            Inputs: session/name/prompt/history/payload/contract/counter/model:
                agent_turn-compatible inputs.
            Returns: Rows plus extraction fields when needed; also records page and reconciliation
                sizes.
            Details: Validates phase-4 subagent payloads, including lookahead/lookbehind, and
                returns controlled conflicting definitions.
            """
            assert model == 'gpt-6-luna'
            if 'reconcile_agent' in name:
                assert set(payload) == {'target_rows'} and not history
                targets = payload['target_rows']
                reconciliation_sizes.append(len(targets))
                rows = []
                for target in targets:
                    first, second = target['source_rows']
                    assert (first['source'], second['source']) == ('table1', 'table2')
                    rows.append(row(target['variable_name'], first['variable_description'], 'Pages 1 and 11')
                                | {'values': second['values']})
                return {'rows': rows}
            if 'merge_agent' in name:
                assert [r['variable_name'] for r in payload['current_page_rows']]==['id']
                assert payload['existing_rows'][0]['variable_description']=='First definition'
                return {'rows':[row('id','First definition\nSecond definition','Pages 1, 2')]}
            current=payload['current_page']['page']
            calls.append(current)
            assert payload['page_range'] == ([1, 10] if current <= 10 else [11, 12])
            assert (payload['next_page_lookahead'] or {}).get('page') == (current + 1 if current < 12 else None)
            assert (payload['previous_page_lookbehind'] or {}).get('page') == (10 if current == 11 else None)
            rows = {1: [row('ID', 'First definition'), row('SAME', location='Page 1'),
                        *(row(name, location='Page 1') for name in related)],
                    2: [row('ID', 'Second definition'), row('NEW')],
                    11: [row('id', 'not in source', 'Page 11') | {'values': '1 - Yes'},
                         row('SAME', location='Page 11'), row('LATER'),
                         *(row(name, 'not in source', 'Page 11') | {'values': '1 - Yes'} for name in related)]}
            return {'rows': rows.get(current, []),
                    'general_information':[f'Information {current}'],'unfinished_entry':'',
                    'issues':['Wrapped index line'] if current==1 else []}
        directory=root/'pdf';directory.mkdir()
        class PdfTransport:
            """Drive the actual checkpoint pipeline with the existing regression replies."""
            model, output_limit, missing_response_error = 'gpt-6-luna', 16000, FileNotFoundError
            def __init__(self):
                self.responses = {}
            def check_stop(self):
                session.check_stop()
            def new_thread(self):
                from uuid import uuid4
                return 'conversation_' + uuid4().hex
            def count_input(self, request, name):
                return 100
            def submit(self, request, name):
                from uuid import uuid4
                payload = json.loads(request['input'][0]['content'])
                result = pdf_reply(session, name, request['instructions'], [], payload, None, {}, request['model'])
                response_id = uuid4().hex
                self.responses[response_id] = {'id': response_id, 'status': 'completed',
                    'output_text': json.dumps(result), 'usage': {'input_tokens': 100, 'output_tokens': 20}}
                return {'id': response_id, 'status': 'queued'}
            def retrieve(self, response_id):
                return self.responses[response_id]
            def progress(self, *args):
                pass
        with patch('v_5.ingest.codebook_ingestion.sub_agents.pdf.read_pdf',return_value=pages):
            table=pdf_codebook(session,{'filename':'book.pdf','path':'unused'},directory, provider=PdfTransport())
        assert set(table.variable_name) == {'id', 'new', 'later', 'same', *related}
        by_name = table.set_index('variable_name')
        assert by_name.loc['id', 'variable_description'] == 'First definition\nSecond definition'
        assert by_name.loc['id', 'values'] == '1 - Yes'
        assert by_name.loc['same', 'source_location'].splitlines() == ['Page 1', 'Page 11']
        notes = (directory/'general_information.md').read_text()
        assert [line for line in notes.splitlines() if line.startswith('## Page')] == [f'## Page {n}' for n in range(1, 13)]
        joined = pd.read_csv(directory/'block_tables_joined.csv')
        assert {'variable_description_table1', 'variable_description_table2'} <= set(joined.columns)
        assert sorted(calls) == list(range(1, 13)) and reconciliation_sizes == [30, 2]
        report=json.loads((directory/'verification.json').read_text())
        assert report['blocks'] == 2 and report['issues']==['Page 1: Wrapped index line']
        history=[{'role':'user','content':'old page'}];counter={'used':249000,'thread':1}
        payload = {'current_page': {'page': 3, 'markdown': 'new page'}}
        try:
            agent_turn(session, 'pdf_codebook_agent — book.pdf', 'prompt', history, payload,
                       PdfCodebookReply, counter, 'gpt-6-luna')
        except RuntimeError as error:
            assert '250,000-token session budget' in str(error)
        else:
            raise AssertionError('An over-budget worker was silently restarted.')
        assert counter['thread'] == 1 and not session.calls
        agent_turn(session, 'pdf_codebook_agent — book.pdf', 'prompt', [], payload,
                   PdfCodebookReply, dict(used=0, thread=1), 'gpt-6-luna')
        assert session.calls[0]['model']=='gpt-6-luna'
        assert session.calls[0]['reasoning']=={'effort':'high'}
        assert session.messages[-1][2]=='thinking' and 'Page 3' in session.messages[-1][1]
        for model in (None, 'gpt-6.1-sol'):
            agent_turn(session, 'codebook_agent', 'prompt', [], payload,
                       PdfCodebookReply, dict(used=0, thread=1), model)
            assert session.calls[-1]['model']=='gpt-6.1-sol'
            assert session.calls[-1]['reasoning']=={'effort':'medium'}

        app=create_app(root/'runtime');client=app.test_client()
        headers={'X-V5-Token':client.get('/api/status').json['token']}
        with patch('v_5.main.rename_atlas_project'):
            pid=client.post('/api/projects',json={'name':'Temporary'},headers=headers).json['id']
        context=dict.fromkeys(['description','source','time_geography','unit'],'Example')
        assert client.post(f'/api/projects/{pid}/upload',headers=headers,data={'role':'primary','dataset_context':json.dumps(context),'files':(io.BytesIO(b'id\n1\n'),'data.csv')}).status_code==200
        for count,expected in [(100,200),(101,400)]:
            pdf=PdfWriter()
            for _ in range(count):pdf.add_blank_page(width=100,height=100)
            stream=io.BytesIO();pdf.write(stream);stream.seek(0)
            result=client.post(f'/api/projects/{pid}/upload',headers=headers,data={'role':'mixed','folder':'data','file_details':json.dumps([{'role':'asset','asset_kind':'codebook'}]),'files':(stream,f'book{count}.pdf')})
            assert result.status_code==expected,result.json
        assert len(client.get(f'/api/projects/{pid}').json['files'])==2
    print('PASS: master parts, joins, PDF block boundaries and reconciliation, session budget and 100-page upload limit. No paid calls.')


if __name__=='__main__':run()
