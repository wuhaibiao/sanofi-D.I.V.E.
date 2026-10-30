"""Build proposal v0.3 from the main snapshot and retained aggregate evaluations."""
import json
from pathlib import Path
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.section import WD_ORIENT, WD_SECTION_START
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT, WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE as RT

OUT = Path('/Users/OleniusBlanc227/Documents/sanofi/memsys/docs/proposal-draft-20261029')
CONFIG = json.loads((OUT/'build-config.json').read_text())
ART = Path(CONFIG['redesign_artifact'])
CATALOG = json.loads((ART/'evaluation-catalog.json').read_text())
DOC = Document()
INK = RGBColor(0, 0, 0)


def font(run, size=None, bold=False, color=INK):
    run.font.name = 'Noto Sans CJK SC'
    run.font.color.rgb = color
    run.font.bold = bold
    if size is not None:
        run.font.size = Pt(size)
    rf = run._element.get_or_add_rPr().find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts')
        run._element.get_or_add_rPr().insert(0, rf)
    for k in ('eastAsia', 'ascii', 'hAnsi'):
        rf.set(qn('w:'+k), 'Noto Sans CJK SC')
    return run


def layout(section, landscape=False):
    section.orientation = WD_ORIENT.LANDSCAPE if landscape else WD_ORIENT.PORTRAIT
    section.page_width = Inches(11 if landscape else 8.5)
    section.page_height = Inches(8.5 if landscape else 11)
    section.top_margin = Inches(.55 if landscape else .62)
    section.bottom_margin = Inches(.55 if landscape else .62)
    section.left_margin = section.right_margin = Inches(.65)
    section.footer_distance = Inches(.22)


layout(DOC.sections[0], True)
styles = [('Normal', 11, 16.5), ('Title', 23, 32), ('Subtitle', 10.5, 16),
          ('Heading 1', 15.5, 24), ('Heading 2', 12, 20)]
for name, size, line in styles:
    st = DOC.styles[name]
    st.font.name = 'Noto Sans CJK SC'
    st.font.size = Pt(size)
    st.font.color.rgb = INK
    st.font.bold = name in ('Title', 'Heading 1', 'Heading 2')
    st._element.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), 'Noto Sans CJK SC')
    st.paragraph_format.line_spacing = Pt(line)
    st.paragraph_format.space_after = Pt(7)
    if name.startswith('Heading'):
        st.paragraph_format.space_before = Pt(8)
        st.paragraph_format.keep_with_next = True
for element in list(DOC.styles.element.iter()):
    if element.tag == qn('w:pBdr'):
        element.getparent().remove(element)
    if element.tag == qn('w:color'):
        for key in ('themeColor', 'themeTint', 'themeShade'):
            element.attrib.pop(qn('w:'+key), None)
    if element.tag == qn('w:rFonts'):
        for key in ('asciiTheme', 'hAnsiTheme', 'eastAsiaTheme', 'cstheme'):
            element.attrib.pop(qn('w:'+key), None)

footer = DOC.sections[0].footer.paragraphs[0]
footer.alignment = WD_ALIGN_PARAGRAPH.RIGHT
font(footer.add_run('技术方案初稿  '), 8)
field = OxmlElement('w:fldSimple')
field.set(qn('w:instr'), 'PAGE')
footer._p.append(field)


def p(text='', lead=None, size=11, after=7):
    para = DOC.add_paragraph()
    para.paragraph_format.space_after = Pt(after)
    if size != 11:
        para.paragraph_format.line_spacing = Pt(size*1.48)
    if lead:
        font(para.add_run(lead), size, True)
    font(para.add_run(text), size)
    return para


def heading(text, level=1, icon=None):
    para = DOC.add_paragraph(style='Heading '+str(level))
    if icon:
        picture = para.add_run().add_picture(str(ART/'icons'/f'{icon}.png'), width=Inches(.22))
        picture._inline.docPr.set('descr', text+'图标')
        para.add_run('  ')
    font(para.add_run(text), 15.5 if level == 1 else 12, True)
    return para


def page(text, landscape=False):
    current = DOC.sections[-1].orientation == WD_ORIENT.LANDSCAPE
    if current == landscape:
        DOC.add_page_break()
    else:
        layout(DOC.add_section(WD_SECTION_START.NEW_PAGE), landscape)
    heading(text)


def link(para, label, url, size=10, color='1B5268'):
    hyp = OxmlElement('w:hyperlink')
    hyp.set(qn('r:id'), para.part.relate_to(url, RT.HYPERLINK, is_external=True))
    run = OxmlElement('w:r')
    prop = OxmlElement('w:rPr')
    rf = OxmlElement('w:rFonts')
    for key in ('eastAsia', 'ascii', 'hAnsi'):
        rf.set(qn('w:'+key), 'Noto Sans CJK SC')
    prop.append(rf)
    col = OxmlElement('w:color'); col.set(qn('w:val'), color); prop.append(col)
    sz = OxmlElement('w:sz'); sz.set(qn('w:val'), str(round(size*2))); prop.append(sz)
    run.append(prop)
    for i, line in enumerate(label.split('\n')):
        if i:
            run.append(OxmlElement('w:br'))
        tx = OxmlElement('w:t'); tx.text = line; run.append(tx)
    hyp.append(run); para._p.append(hyp)


def table(headers, rows, widths, size=10, links=None):
    t = DOC.add_table(rows=1, cols=len(headers))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, width in enumerate(widths):
        t.columns[i].width = Inches(width)
    borders = OxmlElement('w:tblBorders')
    for edge in ('top', 'left', 'bottom', 'right', 'insideH', 'insideV'):
        e = OxmlElement('w:'+edge); e.set(qn('w:val'), 'single')
        e.set(qn('w:sz'), '5'); e.set(qn('w:color'), 'D9D9D9'); borders.append(e)
    t._tbl.tblPr.append(borders)
    for ri, values in enumerate([headers]+rows):
        cells = t.rows[0].cells if ri == 0 else t.add_row().cells
        trpr = cells[0]._tc.getparent().get_or_add_trPr()
        trpr.append(OxmlElement('w:cantSplit'))
        if ri == 0:
            trpr.append(OxmlElement('w:tblHeader'))
        for ci, text in enumerate(values):
            cell = cells[ci]; cell.width = Inches(widths[ci])
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            props = cell._tc.get_or_add_tcPr()
            shade = OxmlElement('w:shd')
            shade.set(qn('w:fill'), '153D56' if ri == 0 else ('F1F6F9' if ri % 2 == 0 else 'FFFFFF'))
            props.append(shade)
            margins = OxmlElement('w:tcMar')
            for side, value in [('top',85),('bottom',85),('left',90),('right',90)]:
                el = OxmlElement('w:'+side); el.set(qn('w:w'), str(value)); el.set(qn('w:type'), 'dxa'); margins.append(el)
            props.append(margins)
            para = cell.paragraphs[0]
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.line_spacing = Pt(14.4 if size >= 10 else 13.6)
            if ri == 0 or (ci > 0 and widths[ci] <= 1.6):
                para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            if ri > 0 and ci == 0 and links:
                link(para, str(text), links[ri-1], size)
            else:
                font(para.add_run(str(text)), size, ri == 0,
                     RGBColor(255,255,255) if ri == 0 else INK)
    p('', size=3, after=2)
    return t


DOC.add_paragraph('长期交互式AI记忆系统技术方案', style='Title')
DOC.add_paragraph('初稿 v0.3    2026年10月30日', style='Subtitle')
heading('一 整体流程')
fig = DOC.add_paragraph()
fig.alignment = WD_ALIGN_PARAGRAPH.CENTER
fig.paragraph_format.space_after = Pt(1)
fig.paragraph_format.line_spacing = 1
picture = fig.add_run().add_picture(str(ART/'memory-workflow.body.png'), width=Inches(9.4))
picture._inline.docPr.set('descr', '主方案流程。历史写入依次保存原始对话、抽取事实并嵌入、发布到Qdrant。当前问题经混合召回和本地BGE重排，选择记忆并组织context，由宿主结合近期对话生成答案。')
p('图1  主方案的主要处理路径  原文、事实与索引均持久保存；图中不表示耗时。', size=9.5, after=5)
para = p(''); link(para, '打开可交互流程图', (ART/'memory-workflow.html').as_uri(), 9.5)
p('系统先把历史保存为可追溯的记忆，再按当前问题取出相关内容。记忆服务负责保存、检索和返回上下文，宿主 Agent 负责最终回答。', size=10.5, after=4)

page('二 技术选型与必要性')
p('基本能力包括跨会话持久保存、来源与时间关联、相关内容检索和宿主接入。具体数据库与模型可以替换，重排属于质量增强。选型依据是这些能力在当前处理链中的作用。')
heading('记忆形式与来源保留', 2, 'memory')
p('长期交互必须区分谁说了什么、哪件事已经发生、哪件事仍是计划，以及信息属于哪个时间。缺少这些关系，系统容易把助手建议当成用户经历，或把不同事件合并。')
p('主方案在写入时抽取结构化事实，并关联原文、角色和时间，供后续检索、状态比较和聚合使用。原文持续保存，使遗漏的信息有重新处理的入口。时间与来源组织参考 LongMemEval 和 Graphiti。[1][5]')
p('原文分块可以省去语义抽取，但读取时需要由 Agent 重新理解事件与状态。本期保留结构化事实作为主方案，分块作为已实测的替代路线；两者的质量与用量在后面的同题表中对照。自动冲突识别仍需完善，已有时间字段不等于更新语义已经完整解决。')
heading('存储采用 Qdrant', 2, 'storage')
p('跨会话复用需要持久存储，按用户隔离和按来源定位也是基本要求。Qdrant 可同时承载正文与元数据、稠密向量及词项稀疏索引，使两路检索和记录维护集中在同一服务。[2]')
p('若已有 PostgreSQL，pgvector 配合全文检索可以沿用 SQL、事务和现有运维；关系路径成为主要查询需求时，图数据库更值得考虑。本期优先完成对话记忆检索，因此选择当前已接通的 Qdrant，避免同时维护多套检索存储。[8]')
heading('嵌入采用 BGE M3 并保留词项召回', 2, 'search')
p('用户会用不同说法询问同一件事，也会直接问人名或名称。语义向量用于匹配改写表达，词项通道补充原词匹配，两路按 RRF 名次融合；仅依赖其中一路会失去另一类匹配信号。[2]')
p('BGE M3 支持多语言，模型输入上限8192 token，为较长对话片段留出余量；实际输入仍按配置分窗。写入时建立向量，提问时编码查询。本期使用其稠密嵌入，词项索引由本地文本处理生成。作为替代的 multilingual e5 large 模型卡上限为512 token，长片段需要更细分块，因此当前优先 BGE M3。[3][9]')

page('三 候选重排与 Agent 接入')
heading('在初步召回后进行本地重排', 2, 'rank')
p('需要控制相关性和重复内容，才能把有限的上下文位置留给有用信息。bge reranker v2 m3 联合读取问题和候选，细化相关性排序；放在初召回之后，可将计算量限制在候选集内。[4]')
p('本地重排无需增加远端生成调用。本期没有采用每题再调一次生成模型筛选的方案：同题实验中，筛选虽然缩短了回答输入，但回答加筛选的成功请求输入增加了20.70%，原始得分也未超过主干。若延迟优先，可采用融合排序或更轻的重排器；目前没有同条件评测证明其他重排模型更优。')
heading('记忆上下文交给宿主 Agent', 2, 'agent')
p('记忆需要能够跨模型复用。服务返回 context，由宿主把它与当前问题、近期对话一起交给回答模型；完整 items 与过程记录供程序处理。这样可以独立调整记忆组织与回答模型，并避免把相同正文以多种字段重复注入。')
p('单个 Agent 可通过同进程库降低接入开销；多个宿主可使用独立 API。Python 便于复用当前嵌入、重排与评测工具，但不是记忆机制的必要条件。元数据选择参考 LlamaIndex，正文与完整文档分开返回参考 LangChain。[6][7]')
heading('开源项目的具体参考关系', 2, 'references')
table(['来源', '参考机制', '对应环节'], [
    ['LongMemEval', '历史索引 检索 阅读及时间推理', '整体流程与评测'],
    ['Graphiti', '原始来源与事实关联 事实时间表达', '记忆组织与状态'],
    ['Qdrant 与 BGE', '稠密和词项混合 检索后交叉编码重排', '召回与排序'],
    ['LlamaIndex', '分别选择嵌入和生成所需元数据', '正文与元数据组织'],
    ['LangChain', '正文和完整文档分别返回', '宿主接入接口'],
], [1.40,3.95,1.85], size=10)
p('重排、来源关联和结构化组织分别改善不同环节。最终效果还取决于写入是否保留必要信息，以及 Agent 是否正确使用已返回内容；单个组件的公开能力不能替代本系统的完整实测。', size=10.5)

page('四 历次评测对照', landscape=True)
p('表1汇总10月25日至29日批次的21轮相关真实评测，包含主干、分支及定向错题复测。相同组号表示原题及顺序相同；不同组得分不直接作为版本优劣证据。日期沿用原记录批次名，所有结果均保留原始判分。', size=10.5, after=5)
headers = ['日期与版本', '题组', '原始\n判分', '正常\n交付', '生产生成\n成功响应', '生产输入 / 输出\n千 token', '平均答题输入\ntoken', '平均读答\n秒']
rows = []
for row in CATALOG['rows']:
    average = '—' if row['mean_answer_input_tokens'] is None else f"{row['mean_answer_input_tokens']:,.0f}"
    elapsed = '—' if row['mean_read_answer_s'] is None else f"{row['mean_read_answer_s']:.2f}"
    rows.append([row['label'], row['cohort'], f"{row['correct']}/{row['total']}",
        f"{row['answered']}/{row['total']}", str(row['production_calls']),
        f"{row['production_input_tokens']/1000:,.3f}\n/ {row['production_output_tokens']/1000:,.3f}",
        average, elapsed])
table(headers, rows, [3.0,.70,.70,.70,.78,1.60,1.20,1.02], size=9.5,
      links=[Path(row['report']).as_uri() for row in CATALOG['rows']])
p('口径  生产生成包含抽取、回答及成功返回的模型筛选，评分和本地嵌入、BGE另计。失败请求的未知用量未补算。模型筛选分支有20次逻辑筛选请求，15次成功返回，生产成功响应共68次。', size=9.5, after=4)
p('含流程失败的批次不展示平均输入和读答耗时，避免把缺失工作当作效率收益。定向2题、3题、7题不能换算为完整评测成绩。表中版本名称可打开原始报告。', size=9.5, after=4)
p('这些是已暴露的 LongMemEval Oracle 开发记录，部分题目重复使用，不能合并为独立样本总准确率。回答与评分采用同模型家族，已有评分一致性疑点；不同时间运行的远端耗时也不能全部归因于架构。', size=9.5, after=2)

page('五 已有结论与参考来源')
heading('评测对当前方案的支持', 2, 'references')
p('主干已在D20组取得19/20、在另一组E30取得24/30，均完整交付；表现随题集变化。先前硬校验版本B30仅交付23/30，说明审计字段不应成为答案交付的硬门槛。移除审计后的C20原始评分20/20仍含个性化使用不足的观察，分数通过不能代替内容复核。')
p('D20的模型筛选、Top 10、两版Top 20和返回层实验均未超过主干原始得分。减少返回内容有助于压缩输入，但来源优先、对象覆盖和事件时间仍需保留；返回层实验的一题流程失败发生在写入阶段，不能全部归因于排版。')
p('原文分块从15/20改善到18/20，仍低于主干19/20；生产生成从53次降到20次，输入从432,194降到119,313 token。该组记录支持省去抽取的用量收益，尚不足以证明质量替代。价格未配置，货币费用未作推算。')
p('下一步应围绕抽取遗漏、候选选择以及已返回事实的使用分别改进，保留通用来源和时间表达，并以固定代码的真实调用检查质量、交付和用量。现有小样本尚不能证明完整长历史场景的泛化效果。', size=10.5)
heading('参考来源', 2, 'references')
refs = [
    ('[1] LongMemEval 官方数据与评测', 'https://github.com/xiaowu0162/LongMemEval'),
    ('[2] Qdrant 混合查询与 RRF', 'https://qdrant.tech/documentation/search/hybrid-queries/'),
    ('[3] BAAI BGE M3 模型说明', 'https://huggingface.co/BAAI/bge-m3'),
    ('[4] BAAI bge reranker v2 m3 模型说明', 'https://huggingface.co/BAAI/bge-reranker-v2-m3'),
    ('[5] Graphiti 来源与时间关系', 'https://github.com/getzep/graphiti/blob/main/README.md'),
    ('[6] LlamaIndex BaseNode 元数据组织', 'https://github.com/run-llama/llama_index/blob/main/llama-index-core/llama_index/core/schema.py'),
    ('[7] LangChain retriever 工具返回形式', 'https://github.com/langchain-ai/langchain/blob/master/libs/core/langchain_core/tools/retriever.py'),
    ('[8] pgvector 向量与全文检索', 'https://github.com/pgvector/pgvector'),
    ('[9] multilingual e5 large 模型说明', 'https://huggingface.co/intfloat/multilingual-e5-large'),
]
for label, url in refs:
    para = DOC.add_paragraph(); para.paragraph_format.space_after = Pt(3)
    para.paragraph_format.line_spacing = Pt(14.2)
    link(para, label, url, 9.5)
p('外部选型资料核对日期为2026年10月30日。主方案流程依据保留的主干源码快照；评测数据与分支结果逐轮链接于表1。', size=9.2, after=0)

DOC.core_properties.title = '长期交互式AI记忆系统技术方案'
DOC.core_properties.subject = '整体流程 技术选型与理由 开源参考 历次评测对照'
DOC.core_properties.author = ''
DOC.core_properties.keywords = 'AI Memory LongMemEval Qdrant BGE 技术方案'
DOC.core_properties.comments = ''
file = OUT/'长期交互式AI记忆系统技术方案初稿.docx'
DOC.save(file)
print(file)
print('Paragraphs', len(DOC.paragraphs), 'Tables', len(DOC.tables), 'Sections', len(DOC.sections),
      'Historical evaluations', len(CATALOG['rows']))
