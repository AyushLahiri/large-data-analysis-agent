"""Build two small manual-test projects. No model calls or database access."""
import json
import shutil
from pathlib import Path
from uuid import uuid4
import pandas as pd
import pyreadstat
from reportlab.pdfgen import canvas
from v_5.app.config import RUNTIME, FORMATS
from v_5.tests.energy_fixture import build_energy_fixture

ROOT = Path(__file__).resolve().parent / 'fixtures'
ROOT.mkdir(parents=True, exist_ok=True)


def register(name, folder, description, paths):
    """Create a local manual-test project from fixture files.

    Inputs: name/folder/description: project labels; paths: source/role/note/continuation
        tuples.
    Returns: New project ID; copies uploads and writes project.json and inputs.jsonl under
        RUNTIME.
    Details: Fixture utility for manual testing, not an offline assertion. Assigns reviewer
        IDs consistently with primary versus supporting roles.
    """
    project_id = uuid4().hex
    target = RUNTIME / 'projects' / project_id
    (target / 'uploads').mkdir(parents=True)
    state = dict(version=2, id=project_id, context=dict(name=name, description=description,
        folder_relationships='One dataset folder. Review the uploaded materials with me.'),
        folders={folder: {'description': description}}, files={}, messages=[], status='draft', pending='', preparations={})
    for index, (source, role, note, continuation) in enumerate(paths, 1):
        file_id = f'file_{index:03d}'
        stored = target / 'uploads' / (file_id + source.suffix)
        shutil.copyfile(source, stored)
        fmt = FORMATS[source.suffix]
        state['files'][file_id] = dict(file_id=file_id, filename=source.name, path=str(stored.resolve()),
            format=fmt, bytes=stored.stat().st_size, folder=folder, role=role, note=note,
            continuation=continuation, ignore=False,
            agent='agent_folder_001_data' if role != 'asset' else f'agent_{source.stem[:5]}_{fmt}_{index:03d}')
    (target / 'project.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
    (target / 'inputs.jsonl').write_text(json.dumps({'action':'fixture_created','state':state})+'\n', encoding='utf-8')
    return project_id


simple = ROOT / 'simple_library'
simple.mkdir(exist_ok=True)
frame = pd.DataFrame({'member_id':[f'{i:04d}' for i in range(1,31)], 'age':[20+i%40 for i in range(30)],
    'branch':[1+i%3 for i in range(30)], 'books_borrowed':[i%9 for i in range(30)],
    'satisfaction':[1+i%5 for i in range(30)], 'monthly_fee':[round(2.5+i%4,2) for i in range(30)]})
pyreadstat.write_sav(frame,str(simple/'library_members.sav'),column_labels={
    'member_id':'Unique library member identifier; preserve four digits', 'age':'Age in completed years',
    'branch':'Registered library branch', 'books_borrowed':'Books borrowed during October 2025',
    'satisfaction':'Member satisfaction with library services', 'monthly_fee':'Monthly membership fee in dollars'},
    variable_value_labels={'branch':{1:'North',2:'Central',3:'South'},'satisfaction':{1:'Very poor',2:'Poor',3:'Fair',4:'Good',5:'Excellent'}})
(simple/'library_dictionary.dct').write_text('dictionary {\n str4 member_id "Library member identifier"\n int age "Age in years"\n byte branch "1 North; 2 Central; 3 South"\n int books_borrowed "Books borrowed in October 2025"\n byte satisfaction "1 Very poor to 5 Excellent"\n double monthly_fee "Membership fee in dollars"\n}\n',encoding='utf-8')
pdf=canvas.Canvas(str(simple/'library_report.pdf'))
for title,lines in [('Library member survey, October 2025',['Fictional training data: 30 members across three branches.','Each row represents one member. member_id identifies members.']),('Survey notes',['Satisfaction uses a five-point scale, from very poor to excellent.','Fees are recorded in dollars. The SPSS dictionary is authoritative.'])]:
 pdf.setFont('Helvetica',16);pdf.drawString(60,760,title);pdf.setFont('Helvetica',11)
 for i,line in enumerate(lines):pdf.drawString(60,720-i*25,line)
 pdf.showPage()
pdf.save()
simple_id=register('Test 1 - Library members','Library members','Thirty library members surveyed in October 2025. One record per member; the SPSS dictionary is authoritative.',[(simple/'library_members.sav','primary','Member survey with embedded variable and category labels.',[]),(simple/'library_dictionary.dct','asset','Supporting dictionary for the library survey.',[]),(simple/'library_report.pdf','asset','Short report about this survey.',[])])

hard = ROOT / 'tough_energy'
build_energy_fixture(hard)
hard_id = register(
    'Test 2 - Household energy continuations', 'Household energy',
    'October 2025 energy records for 174 fictional households in the pilot districts. '
    'The Excel workbook is the primary codebook. The DCT and field report contain supplementary definitions.',
    [
        (hard / 'households.csv', 'primary', 'Household roster; keep the reserved slots.', []),
        (hard / 'electricity.csv', 'continuation', 'Match using household_id; the rows are reversed.', ['columns']),
        (hard / 'building.sav', 'continuation', 'SPSS with an embedded dictionary. The key was omitted; rows follow households.csv order.', ['columns']),
        (hard / 'bakery_orders.csv', 'continuation', 'Another export found alongside the project; please review whether it belongs.', ['columns']),
        (hard / 'energy_codebook.xlsx', 'asset', 'Primary codebook. Six sheets include field notes, core variables and eligibility and recodes for two batteries.', []),
        (hard / 'energy_dictionary.dct', 'asset', 'Secondary variable reference with older definitions.', []),
        (hard / 'energy_field_report.pdf', 'asset', 'Field report with supplementary codebook definitions.', []),
        (hard / 'guitar_notes.md', 'asset', 'A note found in the export folder; unsure whether it belongs.', []),
    ],
)
print(json.dumps({'simple': simple_id, 'tough': hard_id, 'fixtures': str(ROOT.resolve())}))
