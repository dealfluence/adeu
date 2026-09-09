import zipfile

import pytest

from adeu.utils.xml_debug import format_and_sort_xml, get_abstracted_xml_snapshot

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DU = "http://schemas.microsoft.com/office/word/2023/wordml/word16du"
MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"


def xml(inner="<w:t>keep</w:t>", declarations="", attributes=""):
    return f'<w:document xmlns:w="{W}" {declarations} {attributes}>{inner}</w:document>'.encode()


@pytest.mark.parametrize("part", ["word/document.xml", "word/header1.xml", "word/footnotes.xml"])
@pytest.mark.parametrize("inner", ['<w:ins w16du:dateUtc="DATE"/>', "<w16du:example/>"])
def test_root_and_local_w16du_are_equivalent(part, inner):
    root = xml(inner, f'xmlns:w16du="{DU}"')
    local = xml(inner.replace("/>", f' xmlns:w16du="{DU}"/>'))
    actual = format_and_sort_xml(local, part)
    assert actual == format_and_sort_xml(root, part)
    assert f'xmlns:w16du="{DU}"' in actual


def test_unused_w16du_does_not_change_snapshot(tmp_path):
    snapshots = []
    for index, declarations in enumerate(("", f'xmlns:w16du="{DU}"')):
        path = tmp_path / f"{index}.docx"
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("word/document.xml", xml(declarations=declarations))
        snapshots.append(get_abstracted_xml_snapshot(str(path)))
    assert snapshots[0] == snapshots[1]


@pytest.mark.parametrize("part", ["word/document.xml", "word/comments.xml"])
@pytest.mark.parametrize(
    "payload",
    [
        xml('<w:ins w16du:dateUtc="DATE"/>'),
        xml(declarations='xmlns:w16du="urn:wrong"'),
        xml('<w:ins xmlns:w16du="urn:wrong" w16du:dateUtc="DATE"/>', f'xmlns:w16du="{DU}"'),
        xml(declarations=f'xmlns:mc="{MC}" xmlns:w16du="urn:wrong"', attributes='mc:Ignorable="w16du"'),
        b'<w:document xmlns:w="urn:w"><w:t></w:document>',
    ],
)
def test_invalid_xml_or_binding_is_rejected(part, payload):
    with pytest.raises(ValueError, match=part):
        format_and_sort_xml(payload, part)


@pytest.mark.parametrize(
    "before,after",
    [
        (xml("<w:t>keep</w:t>"), xml("<w:t>changed</w:t>")),
        (xml("<w:t>keep</w:t> tail"), xml("<w:t>keep</w:t> other")),
        (xml("<w:t>A</w:t><w:t>B</w:t>"), xml("<w:t>B</w:t><w:t>A</w:t>")),
        (xml('<w:t xml:space="preserve"> keep </w:t>'), xml("<w:t> keep </w:t>")),
        (xml('<w:ins w:author="A"/>'), xml('<w:ins w:author="B"/>')),
        (xml('<w:rStyle w:val="StyleA"/>'), xml('<w:rStyle w:val="StyleB"/>')),
        (xml(), xml().replace(W.encode(), b"urn:wrong-wordprocessingml")),
        (xml('<w:ins w16du:dateUtc="DATE"/>', f'xmlns:w16du="{DU}"'), xml("<w:ins/>")),
        (xml(declarations='xmlns:other="urn:one"'), xml(declarations='xmlns:other="urn:two"')),
    ],
)
def test_real_differences_remain_visible(before, after):
    assert format_and_sort_xml(before, "word/document.xml") != format_and_sort_xml(after, "word/document.xml")


def test_qname_value_scope_is_not_erased():
    bound = xml(declarations=f'xmlns:mc="{MC}" xmlns:w16du="{DU}"', attributes='mc:Ignorable="w16du"')
    unbound = xml(declarations=f'xmlns:mc="{MC}"', attributes='mc:Ignorable="w16du"')
    result = format_and_sort_xml(bound, "word/document.xml")
    assert f'xmlns:w16du="{DU}"' in result
    assert result != format_and_sort_xml(unbound, "word/document.xml")


@pytest.mark.parametrize("inner", ["<w:t>w16du:example</w:t>", "<w:t><![CDATA[w16du:example]]></w:t>"])
def test_text_prefix_reference_keeps_its_namespace_scope(inner):
    local = xml(f'<w:p xmlns:w16du="{DU}">{inner}</w:p>')
    result = format_and_sort_xml(local, "word/document.xml")
    assert f'<w:p xmlns:w16du="{DU}">' in result


@pytest.mark.parametrize("part", ["word/document.xml", "word/comments.xml"])
def test_snapshot_api_propagates_parse_failure(tmp_path, part):
    path = tmp_path / "invalid.docx"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr(part, xml('<w:ins w16du:dateUtc="DATE"/>'))
    with pytest.raises(ValueError, match=f"{part}: invalid XML"):
        get_abstracted_xml_snapshot(str(path))
