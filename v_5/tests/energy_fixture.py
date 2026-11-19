"""Generate the expanded energy test files without creating an app project."""
from csv import DictWriter
from pathlib import Path
from random import Random
from xml.sax.saxutils import escape

import pandas as pd
import pyreadstat
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, Table, TableStyle

ROOT = Path(__file__).resolve().parent / 'fixtures' / 'tough_energy'
ROW_COUNT = 174
EMPTY_RECORDS = {47, 149}  # Zero-based. Keep the join key, even on empty records.
COMFORT_ITEMS = (
    'Living room warmth', 'Bedroom warmth', 'Freedom from drafts',
    'Control over indoor temperature', 'Comfort during cold nights',
)
SAVING_ITEMS = (
    'Turn off lights in empty rooms', 'Use LED bulbs', 'Wash clothes in cold water',
    'Air-dry laundry', 'Switch devices off at the wall', 'Use appliance eco settings',
    'Lower heating overnight', 'Close curtains after dark', 'Seal window drafts',
    'Check meter readings regularly', 'Shift appliance use outside peak hours',
)


def add_batteries(house, rng):
    """Add synthetic survey batteries and derived scores to a household.

    Inputs: house: mutable household record; rng: seeded random generator.
    Returns: None; updates item responses, totals and bands.
    Details: Fixture generation applies survey-mode/tenure eligibility so missing responses
        have a deliberate meaning.
    """
    comfort = [rng.randint(1, 5) for _ in COMFORT_ITEMS] if house['survey_mode'] == 'visit' else []
    saving = [rng.randint(0, 1) for _ in SAVING_ITEMS] if house['tenure'] == 'own' else []
    for prefix, items, responses in [('comfort', COMFORT_ITEMS, comfort), ('saving', SAVING_ITEMS, saving)]:
        house.update({f'{prefix}_{number:02d}': responses[number - 1] if responses else ''
                      for number in range(1, len(items) + 1)})
    house['comfort_total'] = sum(comfort) if comfort else ''
    house['comfort_favorable_count'] = sum(score >= 4 for score in comfort) if comfort else ''
    saving_count = sum(saving)
    house['saving_count'] = saving_count if saving else ''
    house['saving_band'] = ('high' if saving_count >= 7 else 'medium' if saving_count >= 4 else 'low') if saving else ''


def write_table(path, rows, delimiter=','):
    """Write fixture records as a delimited table.

    Inputs: path: destination; rows: nonempty ordered dict records; delimiter: separator.
    Returns: None; writes a header and all rows.
    Details: Used by build_energy_fixture; field order comes from the first record.
    """
    with path.open('w', encoding='utf-8', newline='') as output:
        writer = DictWriter(output, fieldnames=list(rows[0]), delimiter=delimiter)
        writer.writeheader()
        writer.writerows(rows)


def write_energy_report(path):
    """Create the synthetic field report and PDF variable reference.

    Inputs: path: destination PDF.
    Returns: None; writes a deterministic twenty-page report.
    Details: Manual-ingestion fixture includes explanatory text, tables and charts; nested
        layout helpers track vertical space and check footer overlap.
    """
    pdf = canvas.Canvas(str(path), pagesize=(612, 792), invariant=True)
    pdf.setTitle('October household energy: field report and variable reference')
    ink = colors.HexColor('#243747')
    teal = colors.HexColor('#13696c')
    pale = colors.HexColor('#e9f2f1')
    body = ParagraphStyle('body', fontName='Helvetica', fontSize=11, leading=16, textColor=ink)
    cell = ParagraphStyle('cell', parent=body, fontSize=9, leading=12)
    page_number = 0
    y = 0

    def heading(title, section='FIELD REPORT'):
        """Start a fixture-report page with its title and footer.

        Inputs: title: page heading; section: small section label; captures PDF canvas and
            colors.
        Returns: None; advances page_number and resets the vertical writing position.
        Details: Used for each synthetic report page before paragraphs, tables or charts are
            drawn.
        """
        nonlocal page_number, y
        page_number += 1
        pdf.setFillColor(teal)
        pdf.setFont('Helvetica-Bold', 9)
        pdf.drawString(54, 752, 'OCTOBER 2025  /  ' + section)
        pdf.setFillColor(ink)
        pdf.setFont('Helvetica-Bold', 24)
        pdf.drawString(54, 703, title)
        pdf.setFont('Helvetica', 9)
        pdf.drawString(54, 40, 'Household energy pilot  |  Synthetic test material')
        pdf.drawRightString(558, 40, f'{page_number:02d} / 20')
        y = 663

    def paragraph(value, indent=0):
        """Draw wrapped fixture-report text at the current position.

        Inputs: value: paragraph text; indent: left inset; captures canvas and vertical
            position.
        Returns: None; draws text and moves the cursor down.
        Details: Report helper that leaves subsequent tables/paragraphs below the rendered
            block.
        """
        nonlocal y
        block = Paragraph(escape(value), body)
        _, height = block.wrap(504 - indent, 600)
        y -= height
        block.drawOn(pdf, 54 + indent, y)
        y -= 16

    def table(headers, rows, widths=(125, 223, 156)):
        """Draw a styled fixture-report table.

        Inputs: headers: column labels; rows: cell values; widths: column widths.
        Returns: None; draws the table and advances the vertical cursor.
        Details: Wraps cells as paragraphs before measuring the table so the footer check sees
            its true height.
        """
        nonlocal y
        data = [[Paragraph(escape(value), cell) for value in row] for row in [headers, *rows]]
        block = Table(data, colWidths=widths, hAlign='LEFT')
        block.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), pale),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LINEBELOW', (0, 0), (-1, 0), 1, teal),
            ('LINEBELOW', (0, 1), (-1, -1), .35, colors.lightgrey),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        _, height = block.wrap(504, 600)
        y -= height
        block.drawOn(pdf, 54, y)
        y -= 20

    def chart(title, items):
        """Draw a small labeled bar chart in the fixture report.

        Inputs: title: chart heading; items: label/count pairs.
        Returns: None; draws bars and advances the vertical cursor.
        Details: Scales bars against the largest count and uses paragraph for the heading.
        """
        nonlocal y
        paragraph(title)
        for label, count in items:
            pdf.setFillColor(ink)
            pdf.setFont('Helvetica', 10)
            pdf.drawString(74, y, label)
            pdf.setFillColor(teal)
            width = count / max(value for _, value in items) * 250
            pdf.rect(195, y - 2, width, 11, fill=1, stroke=0)
            pdf.setFillColor(ink)
            pdf.drawString(205 + width, y, str(count))
            y -= 23
        y -= 14

    def end():
        """Finish a report page after checking its footer clearance.

        Inputs: Uses the current page number, cursor and PDF canvas.
        Returns: None; raises an assertion on overlap, otherwise starts the next PDF page.
        Details: Called after each synthetic report page is populated.
        """
        assert y >= 56, f'Page {page_number} overlaps its footer'
        pdf.showPage()

    heading('October household energy')
    paragraph('Field report and variable reference for a fictional October 2025 household energy pilot.')
    paragraph('The release combines a household register, an electricity extract and a building worksheet. The plots in this note were drawn from working summaries and may differ from the delivered files.')
    paragraph('Prepared for the internal pilot review. The figures and organizations named here are fictional.')
    end()

    heading('Acknowledgements')
    paragraph('Thank you to the Northbank and Eastbank field teams, the utility records desk, and the interviewers who checked the meter and dwelling forms.')
    paragraph('Mira Chen assembled the building worksheet. Avery Cole reviewed the meter schedules. The pilot team retained responsibility for the release notes.')
    end()

    heading('Index')
    table(['Section', 'What to look for', 'Pages'], [
        ('Scope and collection', 'Sampling, interviews and working counts', '4-5'),
        ('Household register', 'Identifiers, residents and tenure', '6-7'),
        ('Meter extract', 'Consumption, billing, tariff and outages', '8-11'),
        ('Building worksheet', 'Dwelling, fabric and heating', '12-15'),
        ('Field review', 'Rollups, weights and survey batteries', '16-17'),
        ('Legacy fields', 'Export differences and lookup index', '18-20'),
    ])
    end()

    heading('Scope and collection')
    paragraph('The pilot followed occupied dwellings in Northbank, Eastbank and Southbank. The reporting window was October 2025. A household roster and an electricity account were recorded separately.')
    paragraph('The draft briefing counted 172 completed household interviews. A separate register also retained two reserved identifiers for slots with no completed observation.')
    paragraph('The draft summary was circulated before building visits were checked. The team met at the municipal library on 6 November to review the field experience.')
    end()

    heading('Pilot returns')
    chart('Figure 1. Completed interview tally in the working brief', [('Northbank', 58), ('Eastbank', 56), ('Southbank', 58)])
    paragraph('These are field dispatch totals. The review team used the chart to plan callbacks, alongside a separate queue for unreadable meter photographs.')
    table(['Working status', 'Count', 'Note'], [('Completed', '172', 'October roster'), ('Reserved', '2', 'Retained in register')])
    end()

    heading('Household register', 'VARIABLE REFERENCE')
    table(['Field ID', 'Label as printed', 'Storage or coding'], [
        ('HOUSEHOLD_ID', 'Household identifier', 'Character. Preserve the H prefix and four digits.'),
        ('postal_code', 'Postal area', 'Character. Leading zeros are meaningful.'),
        ('Occupants', 'Usual residents', 'Numeric. Excludes visitors staying fewer than 14 nights.'),
        ('region', 'Dispatch district', 'north, east or south'),
    ])
    paragraph('Interview packs were distributed by district on Mondays. Field staff wrote postal_code from the address form and kept zeros at the start. The packs used a green cover sheet.')
    end()

    heading('Tenure and roster notes', 'VARIABLE REFERENCE')
    table(['Variable', 'Question or label', 'Response values'], [
        ('tenure', 'What is your tenure?', 'own = owned; rent = rented; other = other arrangement'),
        ('adults', 'Usual residents aged 18 or older', 'Numeric'),
        ('children', 'Usual residents under 18', 'Numeric'),
        ('survey_mode', 'How the interview was completed', 'phone = phone; visit = in person'),
    ])
    paragraph('A meal break was scheduled before the callback shift. For adults and children, use age at interview and count only usual residents. The callback desk closed at 18:00.')
    end()

    heading('Electricity extract', 'VARIABLE REFERENCE')
    table(['Export key', 'Meaning', 'How recorded'], [
        ('electricity_kwh', 'Metered monthly electricity', 'kWh; October closing meter reading less opening meter reading'),
        ('peak_kwh', 'Electricity during peak periods', 'Numeric kWh'),
        ('OffPeak_KWh', 'Electricity outside peak periods', 'Numeric kWh'),
        ('tariff_band', 'Billing tariff group', 'A = standard; B = time-of-use. See category C on the next page.'),
    ])
    paragraph('The next page completes tariff_band. Please retain its final category and the special blank code.')
    end()

    heading('Meter extract, continued', 'VARIABLE REFERENCE')
    table(['Tariff continuation', 'Code', 'Printed wording'], [
        ('tariff_band', 'C', 'Community assistance rate'),
        ('', '9', 'No tariff supplied; treat as missing'),
    ])
    paragraph('Staff used paper slips when the meter application had no signal. For tariff_band, record the rate printed on the October statement even when the household reported a pending switch. The replacement slips were blue.')
    table(['Field', 'Reading instruction', 'Form entry'], [
        ('estimated_reading', 'Was the reading estimated by the utility?', 'yes; no'),
        ('meter_type', 'Meter installed at the dwelling', 'digital; analog; smart'),
    ])
    end()

    heading('Billing notes and draft totals')
    paragraph('The utility preview lists bill in cents before conversion. The definition of bill in the October extract is whole currency dollars inclusive of credits. Figures in this report exclude open disputes.')
    table(['Draft segment', 'Recorded bills', 'Average bill, dollars'], [
        ('Standard', '59', '64.10'), ('Time-of-use', '56', '68.30'), ('Assistance', '57', '61.40'),
    ])
    paragraph('These were briefing figures. The finance desk retained the original printout in its monthly binder.')
    end()

    heading('Service and local generation', 'VARIABLE REFERENCE')
    table(['Name on extract', 'Definition', 'Handling'], [
        ('outage_hours', 'Total hours without grid service', 'Numeric; add distinct interruption intervals.'),
        ('solar_kwh', 'Metered on-site solar generation', 'Numeric kWh. Leave empty where no production meter was read.'),
        ('payment_status', 'Status of the October bill', 'paid; open; review'),
    ])
    chart('Figure 2. Callback categories, from supervisor notebook', [('Unreadable photo', 12), ('Address check', 7), ('Rate query', 5)])
    end()

    heading('Building worksheet', 'VARIABLE REFERENCE')
    table(['Worksheet alias', 'Recorded item', 'Notes'], [
        ('building_type', 'Dwelling category', 'flat; house; studio'),
        ('built_year', 'Construction year', 'Numeric calendar year'),
        ('floor_area_m2', 'Heated interior floor area', 'Numeric square meters. Exclude garages and open balconies.'),
    ])
    paragraph('The building worksheet was exported in household-register order. The export template suppressed the identifier column. A view used at the field training retained a frozen header.')
    end()

    heading('Heating and insulation', 'VARIABLE REFERENCE')
    table(['Item tag', 'Definition', 'Allowed responses'], [
        ('heating_fuel', 'Main fuel for space heating', 'electric; gas; wood'),
        ('insulation', 'Insulation observed', 'yes; no; 9 = inaccessible'),
        ('cooling_units', 'Count of fixed cooling units', 'Numeric. Exclude portable fans.'),
    ])
    paragraph('On some visits, the team waited for a key. For insulation, use code 9 when the relevant area cannot be inspected; a blank means the question was skipped. Equipment was checked back in on Fridays.')
    end()

    heading('Exterior materials', 'VARIABLE REFERENCE')
    table(['Survey key', 'Question wording', 'Recorded values'], [
        ('roof_material', 'Main roof covering', 'tile; metal; asphalt'),
        ('storeys', 'Number of above-ground floors', 'Numeric. Count a finished attic only when heated.'),
        ('window_glazing', 'Predominant window type', 'single; double. Definition continues overleaf.'),
    ])
    paragraph('window_glazing instructions continue on the next page with the mixed-window rule and special missing code.')
    end()

    heading('Exterior materials, continued', 'VARIABLE REFERENCE')
    paragraph('For window_glazing, where window types are mixed, classify by total glazed area rather than by number of windows. Record 8 when glazing is obscured; this is a special missing code. Ladder access was suspended after the first week.', indent=24)
    table(['Inspection item', 'What it measures', 'Form instructions'], [
        ('retrofit_year', 'Most recent documented thermal retrofit', 'Numeric. Leave blank if no date can be verified.'),
        ('audit_grade', 'Inspector rating of building envelope', 'A = good; B = fair; C = poor'),
    ])
    end()

    heading('Building field debrief')
    chart('Figure 3. Draft visits by dwelling category', [('Flats', 61), ('Houses', 56), ('Studios', 55)])
    paragraph('The supervisor transcribed five additional audit slips after the chart was drafted. The figures shown here are for workload planning.')
    paragraph('The field team requested a shorter inspection checklist for rainy days.')
    end()

    heading('Weights and contact rules', 'VARIABLE REFERENCE')
    paragraph('survey_weight starts at the inverse selection probability and is multiplied by the district callback adjustment. Do not apply the weight twice when combining the roster and meter extract. The district callback team had six desks.')
    table(['Extract name', 'Form label', 'Source note'], [
        ('survey_weight', 'Household survey weight', 'Numeric; calibrated to occupied dwellings'),
        ('consent_code', 'Interview release consent', 'Y = granted; N = declined; P = pending'),
    ])
    paragraph('The five comfort questions (comfort_01 through comfort_05) were asked during in-person visits only. Each rating runs from 1, very poor, to 5, very good. The interview team left the entire block blank for telephone cases.')
    table(['Derived field', 'How it is calculated', 'When it is available'], [
        ('comfort_total', 'Sum the five comfort ratings.', 'All five ratings supplied'),
        ('comfort_favorable_count', 'Count comfort ratings of 4 or 5.', 'All five ratings supplied'),
    ])
    end()

    heading('Legacy export fields', 'VARIABLE REFERENCE')
    table(['Historical name', 'Description', 'Availability note'], [
        ('prepay_account', 'Prepayment account identifier', 'Prototype only; present in the export layout review.'),
        ('estimated_annual_kwh', 'Annualized meter estimate', 'Numeric. Six-month average multiplied by twelve.'),
        ('cookstove_fuel', 'Main cooking fuel', 'electric; gas; wood'),
    ])
    paragraph('The crew printed two versions of the layout. An older review copy remained in circulation when the new worksheet was issued.')
    end()

    heading('Data release notes')
    paragraph('A reserved slot may have only its household identifier. No consumption, billing, or dwelling details should be imputed for reserved slots without checking the collection register.')
    paragraph('An October draft gave bill as the charge before credits. The field review note on page 10 describes the amount as inclusive of credits. Both texts remained in the circulated report.')
    table(['Record group', 'Draft count', 'Release note'], [('Completed', '172', 'Roster is the reference for building order'), ('Reserved', '2', 'Keep their positions in a column continuation')])
    paragraph('The eleven energy-saving action questions (saving_01 through saving_11) were asked only of owners. Zero is No, one is Yes, and blank is a skipped item. The last field briefing also noted that owners could answer even when the utility bill was under review.')
    table(['Release column', 'Derivation', 'Skipped when'], [
        ('saving_count', 'Count Yes answers across the eleven actions.', 'Any action item blank'),
        ('saving_band', '0-3 Yes = low, 4-6 = medium, 7-11 = high.', 'Any action item blank'),
    ])
    end()

    heading('Variable index', 'REFERENCE INDEX')
    table(['Term', 'Definition pages', 'Related material'], [
        ('bill', '10, 19', 'Draft billing totals'),
        ('floor_area_m2', '12', 'Building worksheet'),
        ('insulation', '13', 'Inspection notes'),
        ('survey_weight', '17', 'Callback adjustments'),
        ('tariff_band', '8-9', 'Meter-extract continuation'),
        ('window_glazing', '14-15', 'Exterior inspection continuation'),
        ('comfort_total', '17', 'Home comfort battery'),
        ('saving_band', '19', 'Energy-saving actions battery'),
    ])
    paragraph('End of field report. Retain this note with the October pilot release.')
    end()

    assert page_number == 20
    pdf.save()


def build_energy_fixture(root=ROOT):
    """Generate the synthetic multi-file energy ingestion fixture.

    Inputs: root: output directory, defaulting to the fixture location.
    Returns: None; writes household/electricity CSVs, statistical data, supporting/unrelated
        files, a dictionary and PDF report.
    Details: Uses seeded randomness and add_batteries, write_table and write_energy_report
        to create repeatable manual-test material with deliberate integration challenges.
    """
    root.mkdir(parents=True, exist_ok=True)
    households, electricity, buildings = [], [], []
    rng = Random(2025)
    for i in range(ROW_COUNT):
        key = f'H{i + 1:04d}'
        house = dict(household_id=key, postal_code=f'{100 + i:05d}', occupants=1 + i % 5,
                     region=['north', 'east', 'south'][i % 3], tenure=['own', 'rent', 'other'][i % 3],
                     survey_weight=round(0.8 + (i % 12) / 20, 2), household_income=1800 + i * 37,
                     adults=1 + i % 3, children=i % 3, survey_mode=['phone', 'visit'][i % 2],
                     consent_code='P' if i % 11 == 0 else 'Y', hh_empty='', hh_internal_flag='R' if i % 13 == 0 else '')
        add_batteries(house, rng)
        meter = dict(household_id=key, electricity_kwh=100 + i * 7, bill=round(30 + i * 1.35, 2),
                     peak_kwh=40 + i * 3, offpeak_kwh=60 + i * 4, meter_type=['digital', 'analog', 'smart'][i % 3],
                     tariff_band=['A', 'B', 'C'][i % 3], outage_hours=i % 9, solar_kwh=i % 20 if i % 4 == 0 else '',
                     estimated_reading=['no', 'yes'][i % 2], payment_status=['paid', 'open', 'review'][i % 3],
                     meter_empty='', billing_batch=f'B{i % 3 + 1}')
        building = dict(building_type=['flat', 'house', 'studio'][i % 3], built_year=1980 + i if i < 24 else 1975 + i % 49,
                        insulation=['yes', 'no'][i % 2], floor_area_m2=35 + i % 110,
                        heating_fuel=['electric', 'gas', 'wood'][i % 3], cooling_units=i % 4,
                        window_glazing=['single', 'double', '8'][i % 3], roof_material=['tile', 'metal', 'asphalt'][i % 3],
                        storeys=1 + i % 3, retrofit_year=2010 + i % 15 if i % 3 == 0 else '',
                        audit_grade=['A', 'B', 'C'][i % 3], building_empty='', field_check='repeat' if i % 9 == 0 else '')
        if i in EMPTY_RECORDS:
            for record in (house, meter, building):
                for field in record:
                    if field != 'household_id':
                        record[field] = ''
        households.append(house)
        electricity.append(meter)
        buildings.append(building)

    write_table(root / 'households.csv', households)
    write_table(root / 'electricity.csv', list(reversed(electricity)))
    building_frame = pd.DataFrame(buildings).replace('', None)
    for name in ('built_year', 'floor_area_m2', 'cooling_units', 'storeys', 'retrofit_year'):
        building_frame[name] = pd.to_numeric(building_frame[name])
    pyreadstat.write_sav(building_frame, str(root / 'building.sav'), column_labels={
        'building_type': 'Dwelling type',
        'built_year': 'Year the dwelling was built',
        'insulation': 'Insulation observed during the visit',
        'floor_area_m2': 'Heated interior area in square meters',
        'heating_fuel': 'Main fuel for space heating',
        'cooling_units': 'Number of fixed cooling units',
        'window_glazing': 'Predominant type of window glazing',
        'roof_material': 'Main roof covering',
        'storeys': 'Above-ground floors including heated attics',
        'retrofit_year': 'Most recent documented thermal retrofit year',
        'audit_grade': 'Building envelope inspection grade',
        'building_empty': 'Unused building export field',
    }, variable_value_labels={
        'building_type': {'flat': 'Flat', 'house': 'House', 'studio': 'Studio'},
        'insulation': {'yes': 'Insulation observed', 'no': 'No insulation observed'},
        'window_glazing': {'single': 'Single glazing', 'double': 'Double glazing', '8': 'Obscured'},
        'audit_grade': {'A': 'Good', 'B': 'Fair', 'C': 'Poor'},
    })
    write_table(root / 'bakery_orders.csv', [
        dict(order_id=f'B{i:03d}', pastry=['bun', 'cake'][i % 2], quantity=i % 4 + 1) for i in range(8)
    ])
    (root / 'guitar_notes.md').write_text(
        '# Guitar practice\n\nPractice C, G and D chords for ten minutes.\n'
        'This note is unrelated to household energy or bakery orders.\n', encoding='utf-8')
    # The primary XLSX codebook is a checked-in fixture with deliberately varied sheets.
    dct_definitions = [
        ('str5', 'HOUSEHOLD_ID', 'Unique household identifier'),
        ('int', 'occupants', 'Usual residents; excludes temporary visitors'),
        ('double', 'electricity_kwh', 'October import from grid in kWh'),
        ('double', 'bill', 'October charge in cents before credits'),
        ('str6', 'building_type', 'Dwelling type: flat, house or studio'),
        ('str8', 'insulation', 'yes; no; 9 = inaccessible'),
        ('str5', 'region', 'Field dispatch district'),
        ('double', 'offpeak_kwh', 'Off-peak electricity in kWh'),
        ('str7', 'meter_type', 'digital; analog; smart'),
        ('str8', 'heating_fuel', 'electric; gas; wood'),
        ('int', 'retrofit_year', 'Last retrofit year'),
        ('str1', 'audit_grade', 'A good; B fair; C poor'),
        ('str15', 'prepay_account', 'Prepayment account identifier from the prototype'),
        ('double', 'estimated_annual_kwh', 'Annualized consumption from the historical layout'),
    ]
    dct_definitions += [
        ('byte', f'comfort_{i:02d}', f'{label}: 1 very poor to 5 very good, in-person visits only')
        for i, label in enumerate(COMFORT_ITEMS, 1)
    ]
    dct_definitions += [
        ('byte', f'saving_{i:02d}', f'{SAVING_ITEMS[i - 1]}: 0 no, 1 yes; owners only')
        for i in (1, 3, 11)
    ]
    dct_definitions += [
        ('byte', 'comfort_total', 'Sum of five answered comfort items'),
        ('byte', 'comfort_favorable_count', 'Count of comfort items scored 4 or 5; all five required'),
        ('byte', 'saving_count', 'Count Yes across eleven action items; all eleven required'),
        ('str6', 'saving_band', '0-3 low, 4-6 medium, 7-11 high; all eleven action items required'),
        # Keep the manual reconciler cases when the DCT is regenerated. The primary
        # workbook carries these roots/near-matches instead of four exact item keys.
        ('double', 'householdincome', 'October 2025 monthly gross income of all household members in USD; export spelling separates household and income'),
        ('str1', 'hh_internalflag', 'Retired meter-casing hardware flag: H housing damaged, C cover missing; not a household interview flag'),
        ('str2', 'billingbatch', 'October billing-extract processing group: B1 run one, B2 run two, B3 run three; spelling sometimes separates billing and batch'),
        ('byte', 'saving', 'Owner action battery root: item 02 use LED bulbs; item 04 air-dry laundry; two-digit item number follows root; 0 no, 1 yes; documents only 02 and 04; count and band are derived recodes'),
        ('double', 'saving04', 'Annual USD saving estimated for a laundry-equipment rebate, applicants including renters; monetary estimate, not the air-drying action'),
        ('byte', 'saving06', 'Confidence in using appliance eco settings, separate follow-up for owners and renters: 1 not confident, 2 slightly, 3 moderately, 4 very, 5 completely; not the owner action'),
        ('byte', 'saving08', 'Obsolete pilot revision A item 08; question wording and eligibility missing; archived 0 and 1 meanings unknown; financial saving versus household behaviour unresolved'),
    ]
    declarations = '\n'.join(f' {kind} {name} "{label}"' for kind, name, label in dct_definitions)
    (root / 'energy_dictionary.dct').write_text('dictionary {\n' + declarations + '\n}\n', encoding='utf-8')
    write_energy_report(root / 'energy_field_report.pdf')


if __name__ == '__main__':
    build_energy_fixture()
