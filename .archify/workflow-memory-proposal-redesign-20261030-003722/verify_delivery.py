"""Check document structure, numerical fidelity, and unchanged source records."""
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
from docx import Document
from docx.oxml.ns import qn
from pypdf import PdfReader

ROOT = Path('/Users/OleniusBlanc227/Documents/sanofi')
HERE = Path(__file__).resolve().parent
OUT = ROOT/'memsys/docs/proposal-draft-20261029'
DOCX = OUT/'长期交互式AI记忆系统技术方案初稿.docx'
QA = OUT/'.qa-redesign-verified-20261030'
catalog = json.loads((HERE/'evaluation-catalog.json').read_text())
before = json.loads((HERE/'product-before.json').read_text())
doc = Document(DOCX)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cell_text(cell):
    return ''.join(node.text or '' for node in cell._tc.iter(qn('w:t')))


assert len(doc.tables) == 2
assert len(doc.tables[1].rows) == len(catalog['rows'])+1 == 22
for actual, expected in zip(doc.tables[1].rows[1:], catalog['rows']):
    cells = [cell_text(cell) for cell in actual.cells]
    assert cells[0] == expected['label']
    assert cells[1] == expected['cohort']
    assert cells[2] == f"{expected['correct']}/{expected['total']}"
    assert cells[3] == f"{expected['answered']}/{expected['total']}"
    assert cells[4] == str(expected['production_calls'])
    assert cells[5] == f"{expected['production_input_tokens']/1000:,.3f}/ {expected['production_output_tokens']/1000:,.3f}"
    assert cells[6] == ('—' if expected['mean_answer_input_tokens'] is None else f"{expected['mean_answer_input_tokens']:,.0f}")
    assert cells[7] == ('—' if expected['mean_read_answer_s'] is None else f"{expected['mean_read_answer_s']:.2f}")

source_changed = [name for name, sha in before.items() if digest(ROOT/'memsys'/name) != sha]
evaluation_changed = [name for name, sha in catalog['source_files_sha256'].items() if digest(ROOT/name) != sha]
assert not source_changed and not evaluation_changed
assert doc.tables[1].rows[0]._tr.trPr.find(qn('w:tblHeader')) is not None
assert len(doc.sections) == 4
pdf = PdfReader(next(QA.glob('*.pdf')))
assert len(pdf.pages) == 6
assert len(list(QA.glob('page-*.png'))) == 6
headings = [para.text for para in doc.paragraphs if para.style.name == 'Heading 1']
assert headings == ['一 整体流程','二 技术选型与必要性','三 候选重排与 Agent 接入','四 历次评测对照','五 已有结论与参考来源']
with ZipFile(DOCX) as archive:
    pngs = [name for name in archive.namelist() if name.startswith('word/media/')]
    figure_embedded = digest(HERE/'memory-workflow.body.png') in {
        hashlib.sha256(archive.read(name)).hexdigest() for name in pngs}
    assert figure_embedded
summary = json.loads((HERE/'review-2/memory-workflow.finalize-summary.json').read_text())
assert summary['ok'] and all(value == 'pass' for value in summary['gates'].values())
assert summary['artifact']['sha256'] == digest(HERE/'memory-workflow.html')
assert summary['specification']['sha256'] == digest(HERE/'candidate.json')
result = dict(status='passed', pages=6, evaluation_rows=21, original_scores_preserved=True,
    product_files_checked=len(before), product_source_unchanged=True,
    historical_files_checked=len(catalog['source_files_sha256']), historical_files_unchanged=True,
    repeated_header=True, main_flow_first=True, exact_figure_embedded=True,
    diagram_automated_gates=summary['gates'], docx_sha256=digest(DOCX))
(HERE/'delivery-integrity.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps(result,ensure_ascii=False))
