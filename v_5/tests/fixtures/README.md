# Manual chat projects

Simple: 30 fictional library members, SPSS with embedded labels, supporting DCT and a two-page PDF.

Energy: 174 rows and 58 columns when the three data parts are combined. Match electricity to households by household_id. The keyless building.sav follows households.csv row order and supplies its own embedded codebook. The two CSV data files rely on external documentation. Two reserved records have only their ID, and hh_empty, meter_empty and building_empty are entirely empty. Bakery orders and guitar notes are unrelated.

Use energy_codebook.xlsx as the primary codebook. Four sheets contain scattered ancillary notes. Core variables and Batteries contain the actual definitions, with different headers and starting rows. Home comfort has five items asked during in-person visits. Energy-saving actions has eleven items asked of owners. Each group has two derived recodes and is blank for households skipped by its eligibility question.

The workbook and DCT differ on bill, occupants and insulation. Each documents some variables the other omits, and both include historical fields absent from the data. The DCT and the 20-page PDF include shorter battery references. The tariff_band and window_glazing definitions still cross PDF page boundaries. Data fields such as billing_batch and field_check are absent from all three sources.

Run python -m v_5.tests.energy_fixture to regenerate the data, DCT and report without creating app projects. The workbook is a saved test fixture.

## Checking a chart in the Energy chat

After publishing Energy, ask for separate tables of household counts by income
group and by home comfort response. Click **Visualize** above the income table.
Open **View associated table** and compare its row labels and counts with the
income table in the original answer. Then ask in the same chat for the income
groups to appear in descending order of count. The revised chart should open
against the same source table.

### Energy master-reconciler cases

Workbook revision 3 and the DCT include seven deliberate soft-match cases. Upload the updated codebooks and ingest them afresh to see the new definitions. Data files and the PDF are unchanged. These are expected decisions for manual comparison, not live model results.

| Master variable | Candidate source key | Expected decision |
| --- | --- | --- |
| `household_income` | `householdincome` | Accept the documented monthly gross household income; keep the net-income lookalike separate. |
| `billing_batch` | `billingbatch`, possibly `billing` | Accept the B1/B2/B3 processing group. Reject the annual invoice-total meaning. |
| `hh_internal_flag` | `hh_internalflag` | Reject: retired meter hardware flags are not household interview flags. |
| `saving_02` | `saving` | Accept root item 02: LED bulbs, owners, 0 No / 1 Yes; retain the action battery. |
| `saving_04` | `saving`, `saving04` | Accept root item 04: air-dry laundry. Reject the annual rebate-saving amount. |
| `saving_06` | `saving06`, possibly `saving` | Reject the separate confidence scale and population. The root documents only items 02 and 04. |
| `saving_08` | `saving08`, possibly `saving` | Leave unchanged: the obsolete item has no usable wording, code meanings or eligibility. |

The count/band recodes remain standalone, and the existing comfort battery remains separate. The shared `saving` source row can support two master items; rejected near-matches should remain in the unmatched source table. The four selected action keys are absent as explicit variable IDs from all codebooks; the PDF still describes the broader eleven-item battery. If extraction expands that range into exact-name rows, those rows correctly bypass the soft-matching stage—check the source tables when interpreting the run.
