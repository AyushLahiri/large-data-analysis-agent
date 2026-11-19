"""Check that semantic search fetches subthemes only for the selected theme."""

from types import SimpleNamespace
from unittest.mock import Mock

from v_5.analyse.sql_assets.catalog import VariableSearch, candidate_rows


def run():
    """Check scoped variable discovery and battery expansion offline.

    Inputs: No arguments; builds a small fake catalog and search responses.
    Returns: None; prints success or raises an assertion.
    Details: Exercises theme/subtheme selection, search allowances, battery-only filters and
        full definitions without database or embedding calls.
    """
    catalog = SimpleNamespace(
        folders={
            'Survey': {'name': 'Survey', 'themes': ['Geography', 'Energy']},
            'Records': {'name': 'Records', 'themes': []},
        },
        subthemes=Mock(return_value=['Region']),
    )
    picker = VariableSearch(catalog, Mock())
    picker.weight_candidates = Mock(return_value=[])
    picker.matches = Mock(return_value=[{'variable_name': 'region'}])
    term = {'need': 'geography', 'keyword': 'region', 'folder': 'Survey', 'theme': 'Geography', 'subtheme': 'Region'}
    assert 'Request the subthemes' in picker.search([term])[0]['error']
    assert 'Choose an available theme' in picker.search([{**term, 'theme': '', 'subtheme': ''}])[0]['error']
    picker.matches.assert_not_called()
    picker.weight_candidates.assert_not_called()

    choice = {'folder': 'Survey', 'theme': 'Geography'}
    assert picker.get_subthemes([choice, choice]) == [{**choice, 'subthemes': ['Region']}]
    assert picker.get_subthemes([choice]) == [{**choice, 'subthemes': ['Region']}]
    catalog.subthemes.assert_called_once_with('Survey', 'Geography')
    assert 'outside the selected scope' in picker.get_subthemes([{**choice, 'folder': 'Other'}])[0]['error']
    assert 'Choose a subtheme' in picker.search([{**term, 'subtheme': ''}])[0]['error']
    assert 'Choose a subtheme' in picker.search([{**term, 'subtheme': 'Town'}])[0]['error']
    picker.matches.assert_not_called()

    assert picker.search([term])[0]['candidates'] == [{'variable_name': 'region'}]
    picker.matches.assert_called_once_with('Survey', 'region', 'Geography', 'Region', battery_only=False)
    assert 'already searched' in picker.search([term])[0]['error']
    assert picker.search([{**term, 'folder': 'Records', 'theme': '', 'subtheme': ''}])[0]['candidates']
    picker.matches.assert_called_with('Records', 'region', '', '', battery_only=False)

    topics = {'Energy': ['Cooking', 'Lighting']}
    catalog.battery_topics = Mock(return_value=topics)
    battery_selection = {'folder': 'Survey', 'theme': '', 'battery_only': True}
    assert picker.get_subthemes([battery_selection]) == [{**battery_selection, 'themes': topics}]
    assert picker.get_subthemes([{**battery_selection, 'theme': 'Energy'}])[0]['subthemes'] == topics['Energy']
    assert picker.search([{**term, 'battery_only': True}])[0]['candidates']
    picker.matches.assert_called_with('Survey', 'region', 'Geography', 'Region', battery_only=True)
    assert picker.search([{**term, 'theme': '', 'subtheme': '', 'battery_only': True}])[0]['candidates']
    picker.matches.assert_called_with('Survey', 'region', '', '', battery_only=True)

    names = [f'q{i:02}' for i in range(12)]
    rows = [{'variable_name': name, 'variable_description': f'Question {name}', 'battery_summary': 'Energy use',
             'battery_members': [member for member in names if member != name], 'categories': '1 Yes\n2 No'}
            for name in names]
    candidates = candidate_rows(rows)
    assert len(candidates) == 1 and candidates[0]['battery_members'] == names
    assert candidates[0]['variable_description'] == 'Energy use'
    catalog.codebook = Mock(return_value=rows)
    expanded, = picker.show_all_battery('Survey', [candidates[0]['battery_members']])
    assert catalog.codebook.call_args.args[2] == (names,)
    assert [row['variable_name'] for row in expanded['variables']] == names
    assert expanded['variables'][0]['variable_description'] == 'Question q00'
    assert expanded['variables'][0]['categories'] == '1 Yes\n2 No'
    print('PASS: theme search, optional battery-only filters, coalescing, and full battery expansion.')


if __name__ == '__main__':
    run()
