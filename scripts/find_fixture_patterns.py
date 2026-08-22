import glob
import re
from collections import Counter, defaultdict

py_tests = glob.glob('python/tests/**/*.py', recursive=True)
ts_tests = glob.glob('node/packages/core/src/**/*.test.ts', recursive=True)

all_files = py_tests + ts_tests

print(f"Scanning {len(all_files)} test files...")

file_line_counts = {}
for p in all_files:
    with open(p, 'r', encoding='utf-8', errors='ignore') as f:
        file_line_counts[p] = len(f.readlines())

top_files = sorted(file_line_counts.items(), key=lambda x: x[1], reverse=True)[:20]
print("\nTop 20 Largest Test Files:")
for path, lines in top_files:
    print(f"  {lines:<5} lines: {path}")

# Pattern Analysis
xml_ns_declarations = 0
sdt_pr_blocks = 0
alias_tag_blocks = 0
sdt_id_blocks = 0
checkbox_xml_blocks = 0
date_xml_blocks = 0
combo_xml_blocks = 0
doc_mapper_project_calls = 0
engine_apply_single_calls = 0
raw_xml_parse_calls = 0

xml_ns_str = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'

file_pattern_occurrences = defaultdict(Counter)

for path in all_files:
    with open(path, 'r', encoding='utf-8', errors='ignore') as f:
        text = f.read()

    # 1. Namespace attribute strings repeated in raw XML literals
    ns_count = text.count(xml_ns_str)
    if ns_count:
        file_pattern_occurrences[path]['1. Repeated xmlns:w namespace declarations'] = ns_count

    # 2. w:sdtPr blocks with w:id and w:alias
    sdtpr_count = len(re.findall(r'<w:sdtPr>.*?</w:sdtPr>', text, re.DOTALL))
    if sdtpr_count:
        file_pattern_occurrences[path]['2. Repeated <w:sdtPr> blocks'] = sdtpr_count

    # 3. Checkbox SDT XML blocks (<w14:checkbox> ... </w14:checkbox>)
    chk_count = len(re.findall(r'<w14:checkbox>.*?</w14:checkbox>', text, re.DOTALL))
    if chk_count:
        file_pattern_occurrences[path]['3. Checkbox SDT XML blocks (<w14:checkbox>)'] = chk_count

    # 4. Date SDT XML blocks (<w:date ...> ... </w:date>)
    date_count = len(re.findall(r'<w:date\b.*?>.*?</w:date>', text, re.DOTALL))
    if date_count:
        file_pattern_occurrences[path]['4. Date SDT XML blocks (<w:date>)'] = date_count

    # 5. Dropdown / Combobox SDT XML blocks (<w:dropDownList> / <w:comboBox>)
    combo_count = len(re.findall(r'<(?:w:dropDownList|w:comboBox)\b.*?>.*?</(?:w:dropDownList|w:comboBox)>', text, re.DOTALL))
    if combo_count:
        file_pattern_occurrences[path]['5. Combo/Dropdown SDT XML blocks'] = combo_count

    # 6. Raw DocumentMapper projection setup in tests
    dm_proj = len(re.findall(r'DocumentMapper\(.*?\)', text))
    if dm_proj:
        file_pattern_occurrences[path]['6. DocumentMapper setup calls'] = dm_proj

    # 7. RedlineEngine apply_edits / _apply_single_edit setup in tests
    re_app = len(re.findall(r'RedlineEngine\(.*?\)', text))
    if re_app:
        file_pattern_occurrences[path]['7. RedlineEngine setup calls'] = re_app

    # 8. parse_xml / parseFastXml calls
    px_calls = len(re.findall(r'(?:parse_xml|parseFastXml)\(', text))
    if px_calls:
        file_pattern_occurrences[path]['8. parse_xml / parseFastXml calls'] = px_calls

    # 9. w:ins / w:del tracked changes XML blocks in tests
    tc_count = len(re.findall(r'<w:(?:ins|del)\b.*?>.*?</w:(?:ins|del)>', text, re.DOTALL))
    if tc_count:
        file_pattern_occurrences[path]['9. <w:ins>/<w:del> Tracked Changes XML blocks'] = tc_count

    # 10. w:commentRangeStart / w:commentRangeEnd blocks in tests
    com_count = len(re.findall(r'<w:commentRangeStart\b.*?>', text))
    if com_count:
        file_pattern_occurrences[path]['10. Comment anchor XML blocks (<w:commentRangeStart>)'] = com_count

totals = Counter()
for path, counts in file_pattern_occurrences.items():
    for pat, count in counts.items():
        totals[pat] += count

print("\n--- Pattern Occurrences Across All Test Files ---")
for pat, total in totals.most_common():
    print(f"{pat:<55}: {total} occurrences")
