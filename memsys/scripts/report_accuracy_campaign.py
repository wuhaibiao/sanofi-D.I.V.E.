"""Summarize frozen accuracy runs without scoring unfinished experiments."""
import argparse
import collections
import json
from pathlib import Path


def load(path):
    return json.loads(path.read_text()) if path.exists() else None


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def collect(run):
    freeze = load(run / 'freeze.json') or {}
    answers = rows(run / 'current/hypotheses.jsonl')
    summary = load(run / 'summary.json')
    completed = len({answer['question_id'] for answer in answers})
    sample = load(run / freeze.get('sample_file', 'sample100.json')) or []
    expected = {row['question_id'] for row in sample}
    error_rows = rows(run / 'current/errors.jsonl')
    unresolved = {error['question_id'] for error in error_rows} - {answer['question_id'] for answer in answers}
    stages = freeze.get('stages', [])
    success = all(any(stage['stage'] == name and stage['returncode'] == 0 for stage in stages)
                  for name in ('run', 'judge', 'report'))
    result = summary.get('overall', summary) if summary else {}
    score = {key: result.get(key) for key in ('correct', 'total')}
    verified = (success and bool(expected) and len(expected) == len(sample)
                and {answer['question_id'] for answer in answers} == expected
                and score.get('total') == len(expected))
    modes = collections.Counter()
    for path in (run / 'current/traces').glob('*.json'):
        trace = load(path)
        modes[trace.get('aggregation', {}).get('mode', trace.get('mode', 'unknown'))] += 1
    mechanism = load(run / 'mechanisms/report.json')
    reader_check = load(run / 'live-recovery-check/report.json')
    if not reader_check:
        detail = next((value for name in ('live-check', 'live-bound-check', 'live-format-replay')
                       if (value := load(run / name / 'report.json'))), None)
        if detail:
            cases = detail.get('results', [])
            checks = [case for case in cases if case.get('passed') is not None and 'error' not in case]
            reader_check = dict(mode=detail.get('mode'), total=len(checks),
                                passed=sum(case.get('passed') is True for case in checks),
                                call_errors=sum('error' in case for case in cases),
                                ungraded=sum(case.get('passed') is None for case in cases))
    subset_dir = run / 'live-memory-check'
    subset_info = load(subset_dir / 'provenance.json')
    subset_answers = rows(subset_dir / 'current/hypotheses.jsonl')
    subset_labels = []
    for label_path in sorted((subset_dir / 'current').glob('hypotheses.jsonl.eval-results-*')):
        subset_labels.extend(rows(label_path))
    execution = load(run / 'execution-status.json') or {}
    config = load(run / 'config.frozen.json') or {}
    return {
        'run': run.name, 'completed_answers': completed,
        'sample_size': len(expected),
        'scored_complete': verified, 'score': score if verified else None,
        'types': summary.get('types') if verified else None,
        'paired': summary.get('paired') if verified else None,
        'mode': freeze.get('evaluation_mode', 'fresh_ingestion'),
        'trace_modes': dict(modes), 'stages': stages,
        'cache_validation': freeze.get('cache_validation'),
        'recorded_errors': len(error_rows), 'unresolved_error_questions': len(unresolved),
        'mechanisms': {key: mechanism[key] for key in ('total', 'passed', 'incorrect', 'errors', 'unstarted')}
                      if mechanism else None,
        'live_reader_check': {key: reader_check[key] for key in ('mode', 'total', 'passed', 'call_errors', 'ungraded') if key in reader_check}
                             if reader_check else None,
        'live_memory_subset': dict(mode=subset_info.get('mode'), answered=len(subset_answers),
            graded=len(subset_labels), correct=sum(bool(row.get('autoeval_label', {}).get('label')) for row in subset_labels))
            if subset_info else None,
        'automatic_label_anomalies': summary.get('automatic_label_anomalies') if summary else None,
        'source': freeze.get('source'), 'config_sha256': freeze.get('config_sha256'),
        'sample_sha256': freeze.get('sample_sha256'), 'roles': config.get('roles'),
        'execution_status': execution.get('status'),
        'memory_budgets': {key: config.get('memory', {}).get(key) for key in
                          ('budget_mode', 'context_budget_tokens', 'answer_input_budget_tokens',
                           'memory_input_budget_tokens', 'complete_episode_context')},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=Path, default=Path('runs'))
    parser.add_argument('--out', type=Path, default=Path('docs/accuracy-campaign-v42.md'))
    args = parser.parse_args()
    versions = (21, 22, 23, 32, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69)
    data = []
    for version in versions:
        candidates = sorted(args.runs.glob(f'accuracy-v{version}*'))
        for run in candidates:
            if (run / 'freeze.json').exists():
                data.append(collect(run))
    lines = [
        '# 记忆正确率迭代记录', '',
        '每轮保留冻结源码、配置、固定题号、原始答案、评分及错误日志。未完成全部题目和评分的运行不报告正确率。历史批次为100题；按用户后续要求，新增整批评测默认40题且只在必要时启动。', '',
        '| 运行 | 已答 | 完整正确数 | 跨会话 | 知识更新 | 时间推理 | 评估路径 |',
        '|---|---:|---:|---:|---:|---:|---|',
    ]
    for item in data:
        score = item['score']
        types = item['types'] or {}
        counts = [f"{types[k]['correct']}/{types[k]['total']}" if k in types else '—'
                  for k in ('multi-session', 'knowledge-update', 'temporal-reasoning')]
        score_text = (f"{score['correct']}/{score['total']}" if score else
                      '未启动，已停止' if item['execution_status'] == 'cancelled_before_benchmark' else '未完成')
        label = f"[{item['run']}](../runs/{item['run']}/assessment.md)" if score and item['mode'] != 'full_context_diagnostic' else item['run']
        lines.append(f"| {label} | {item['completed_answers']} | {score_text} | {' | '.join(counts)} | {item['mode']} |")
    adoption = load(args.runs / 'accuracy-campaign/adoption.json')
    candidate = load(args.runs / 'accuracy-campaign/live-candidate.json')
    if candidate and (not adoption or candidate.get('source_sha256') != adoption.get('source_sha256')):
        lines += ['', '## 当前候选状态', '',
                  f"候选：`{candidate['run']}`；状态：`{candidate['status']}`。候选验证与下面的历史采用记录分别保留，未完成评分的候选不报告正确率。", '',
                  f"候选源码：`{candidate.get('source_sha256')}`；冻结配置：`{candidate.get('config_sha256')}`。"]
    if adoption:
        lines += ['', '## 本轮采用结果', '', adoption['decision'], '',
                  adoption['comparison'], '', adoption['validation'], '',
                  adoption['limitations'], '',
                  f"该采用记录源码：`{adoption['source_sha256']}`；配置：`{adoption['config_sha256']}`。", '']
    lines += [
        '', '## 比较口径', '',
        '- 优化样本共100题：跨会话40、知识更新20、时间推理20、用户事实8、助手事实6、偏好6。它是Oracle诊断样本，不能外推正式500题成绩。',
        '- v21–v23原始分数使用GPT评分；v23同一批答案用Step重新评分为62/100。之后读路径对照使用固定Step评分，跨评分模型的分数不能直接归因于代码。',
        '- v32–v39使用优化样本。缓存对照复用同一不可变写入库，并要求100次写入缓存命中；不同回答模型、预算或源码同时变化时，不单独归因于一个改动。',
        '- 同模型、同预算的v34/v35对照为78→72，新增正确2题、回退8题，未证明新筛选逻辑带来稳定收益。8个回退题均没有进入有界压缩路径；39个完全相同的答案评分一致，因此也不能把这些回退直接归因于未执行的压缩代码。',
        '- v40–v45使用另一批100题，与优化样本零重叠。v40从空库写入；v41–v45复用v40的冻结库。该样本首次选定时48题没有历史答案，其余52题曾在历史运行出现，属于独立诊断样本而非盲测。',
        '- v44/v45保存了精简证据候选配置：证据8KB、输入16KB，关闭完整原文路径；原100题计划已停止。v46沿用v44配置，改为40题必要验证。',
        '- 用户后续要求只在必要时评测。未启动的v41/v42/v43/v45预算与推理参数探索已停止，冻结配置保留，不产生分数或采用结论。v41已完成的7个机制用例保留。',
        '- v48只调整离散类别合计的凭据选择提示词；原有字面quantity_label、来源、重复、数量与算术校验均保留。与v47同题同库同模型同预算，并用1个自拟案例验证。',
        '- v49修正剩余量、完成进度序数、记录年份及跨轮活动条件，仍用同一40题和记忆库。自动评分为33/40，跨会话13/16、知识更新8/8、时间推理5/8；相对v47新增正确3题、回退1题。数字标签异常另记，保留原始评分。',
        '- v50针对已观察到的截断和凭据失败，增加包含推理额度的有界重试、类别数量修正、明确来源年份、显式近似月数及字面商品识别。读取与查询使用同一Step模型的low推理参数；评分与写入配置保持原样，因此结果是整组修复及读取参数的对照，不能单独归因于其中一项。自拟读取检查不包含记忆写入，不能替代端到端准确率。',
        '- v50自拟读取检查暴露了序数合计仍不通过，因此在题库评测启动前停止，没有40题分数。其原始名称出现检查将一个以拒答开头的商品描述判为通过，另存审计注明该检查不能证明预期识别行为。v51修正英文规则复数、明确累积序数的字面求和和商品识别表达，并仅重跑受影响的自拟案例。',
        '- v51完整评分34/40（85%）：跨会话13/16、知识更新8/8、时间推理7/8。相对v49新增正确5题、回退4题；约数合计、年份合计和两道偏好题仍需处理。',
        '- v52保留合法数值凭据供语义修正、记录修正失败的原始凭据，新增仅有年份的近似区间及from/to路由，恢复读取模型的默认推理。年份自拟检查通过，约数请求发生连接中断；两道旧偏好题的上下文复查均被自动评分为错误，因此在40题启动前停止。原始失败、补测与评分分别保留。',
        '- v53在原文旁为建议类问题添加预算允许的用户原话索引，并检查具体格式、平台、排除条件及近况；索引保留原角色和计划措辞，超过预算便省略索引，完整原文仍保留。先复查四道受影响旧错题，开发子集不作为整批或独立样本分数。参考答案中缺少原文支持的个人经历另存疑点，不补写入记忆。',
        '- v53开发子集评分3/4，两道偏好和约数合计通过；年份题的引用与10年计算已通过来源检查，却因答案漏写近似词被拦截。因此不再启动该版本40题，修复后合并验证。',
        '- v54允许原始用户已完成事件的记录日仅作为最晚发生日，结合明确终点算出至少多少天，并保留确切间隔未知。自拟下界案例通过；旧时间错题仍评分为错误，原始标签不改。小预算修正测试暴露规则占用过多后，压缩规则并重建，425项测试通过。',
        '- v55仅在来源、运算、结果已通过且唯一问题是缺少精度限定词时，给裸数值或单位答案补近似或下界措辞。保留修正前答案，复查同一凭据，再做语义核对；数值错、来源错、单位错、精确区间冲突及实质文本均不能走格式修正。432项测试通过；旧年份凭据的真实模型复核单独记录，不当作新题正确率。',
        '- v55完整评分35/40（87.5%），跨会话15/16、知识更新8/8、时间推理6/8。相对v51新增正确3题、回退2题。下界引导干扰普通月份估算；商品识别额外说明品牌未记录；一处建议修正丢失已支持的条件分析。完整原始标签和失误日志均保留。',
        '- v56把下界限制到明确请求的最少天数/周数，普通月份/年份题允许根据刚完成的措辞给近记录日期的估算；商品识别直接给已知描述和来源，建议修正保留支持的内容并处理未支持的个人行动。433项测试通过，三道开发错题全部评分通过；自拟最少天数检查因字段放到端点而失败，所以没有启动40题。',
        '- v57明确下界字段位于外层计算对象，并让格式错误指出具体位置。原始来源、请求范围、时钟、数值与语义校验不变；不把无效凭据自动规范化为通过结果。补测回答至少21天，来源、算术及语义核对均通过；自拟脚本因额外要求准确间隔未知的文字仍判失败，原始结果和启动理由分别保留。合并验证沿用同一40题；本地嵌入服务恢复及未完成题补跑单独记录。',
        '- v57完整评分36/40（90%），跨会话14/16、知识更新8/8、时间推理6/8、用户事实3/3、助手事实2/2、偏好3/3。相对v55修复3题、回退2题：完整周被误当成精确天数，名词后数字的累计编号未获解析。原嵌入服务停止造成18条失败；恢复同版本同摘要后补跑，先前22个答案逐字保留，40题缓存命中和Boolean评分完整。',
        '- v58针对v57完整周结果被复核当成精确天数、改成错误单位后拒答的问题，保留程序计算的天数、完整周数和余数，并仅给来源及数值已通过的裸周数补完整周限定。实质文本、错误值、错误来源及只要数字的格式不自动改写；事件语义复核仍保留。440项测试通过，自拟检查通过，实际开发题1/1评分通过。整批已启动10题后，v57完整审计确认另一处编号解析错误，于是停止并合并修复；没有v58整批分数。',
        '- v59增加显式累计进度声明下的名词后整数/英文编号解析，支持求和与字面分组计数；来源、单位、重复及算术检查不变，排名、偏好和计划仍需语义复核拒绝。新增检查覆盖编号、数值子串、小数、累计声明和单位混用，446项测试通过。先复查已观察的跨会话合计错误，再决定整批验证。评估启动脚本检查本地嵌入端口，避免服务停止时继续发出云端查询。',
        '- v59实际合计题给出约27集，来源、算术与语义复核通过，自动评分却为错误；原始No标签保留并单列评分疑点。工具调用未检查断言命令的退出码便继续启动整批，发现后立即停止，保留4个部分答案，不报告整批分数。',
        '- v60让跨对象合计先给总数，再简短列出各对象贡献，保留来源的估计限定，并同步编号规则说明。提示词长度触发一次小预算回归后压缩至2549字节，保留原失败日志；重建、446项测试及泄漏检查通过。新开发答案给出总数和两个分项，1/1自动评分通过；不重评或改写v59的原负标签。启动整批前单独确认成功退出码与已完成的正标签。',
        '- v60完整评分36/40（90%），跨会话14/16、知识更新7/8、时间推理7/8。相对v57修复播客累计数和完整周数，回退午餐总数和电影数量。午餐凭据计算8通过，但答案把所有餐数都称为午餐；电影的5部被写成一个去重类别名，两次后续修正又遇到上游429。原始答案与标签保留，未启动另外40题。',
        '- v61明确报告数量须用求和或分组大小，不能把类别名称当成多个命名实体；所有贡献使用有证据的共同单位，分项沿用来源名称，不添加未证实的子集限定。保持来源、去重、算术和语义复核，规则压缩至2538字节；先重建和检查，再复查两道已观察的回退题。',
        '- v61开发复查1/2：电影的最新5部通过来源、算术、语义及自动评分；午餐修正仍使用sum并把来源名词写成不同item.unit，校验拒绝，整批未启动。规则压缩时遗漏CALCULATION冒号的初次测试失败记录保留，恢复标记后446项全部通过。',
        '- v62在初次规则和数量修正指令中明确所有item.unit等于外层unit，来源名词放入各自quantity_label；数量限定也须适用于总数，不能从问题措辞直接继承。对混用单位也提供分组提示，钱款和时间仍禁止使用分组计数。规则2521字节；仅补查尚失败的开发午餐题，再决定完整40题。',
        '- v62午餐开发补查1/1通过后，完整同题对照达到38/40（95%）：跨会话15/16、知识更新8/8、时间推理7/8，其他题型全部通过。相对v60修复2题、没有回退；全部40题缓存命中、BGE重排和Boolean评分完整，数字标签冲突0题。两个剩余负标签涉及未明确取得的唱片和未记录的取消日期，原始标签保留。',
        '- v62达到同题门槛后，按此前登记的seed261011和固定题型配额，从v40剩余60条记录中抽取另外40题。核对题号零重叠、抽样与预登记一致、源码及配置与已通过版本完全相同；所有记录已有历史答案，因此这是无重叠诊断而非盲测或全量500题成绩。独立批次未完成前不采纳默认配置，也不宣称整体目标完成。',
        '- v62无重叠批次完整评分35/40（87.5%）：跨会话13/16、知识更新8/8、时间推理7/8、用户事实2/3，助手事实及偏好全部通过。40题缓存、BGE重排及Boolean评分完整，数字标签冲突0题。第一组95%未在另一组保持，目标仍未完成，默认配置未采用。',
        '- v63针对第二组已观察错误，允许同一单位内混合字面分组数量与值为1的命名个体，并对身份去重；新增实际出席日的字面日期集合计数和分页读取，不把占用的日期当成连续24小时。保留所有来源、单位、算术与语义核对，并保持月年报告的原始精度，数量回答简短列出已知对象。重建、461项测试及泄漏检查通过，先复查4个开发错题。车辆反馈的发生日及计划测试的实际完成日未被来源确认，不为匹配参考答案编造日期。第二组此后用于修复验证，后续分数明确作为固定开发样本对照，不能再称为新独立验证。',
        '- v63开发复查3/4：鱼缸、出席日期和相机精度通过；婚礼数量及身份仍评分为错误。初次4/4启动断言失败，整批没有被连带启动。之后检查全部原文，发现婚礼参考中的一个配偶名字没有任何来源出现，保存source-review并保留原负标签。三类实际来源修复已通过，因此单独启动固定40题量化修复与回退；不重评同一答案、不补写缺失名字。鱼缸这次走状态读取，仅通过来源与语义核对；日期集合则实际通过程序数值校验。',
        '- v64针对v63冻结评估中出现的一月记录十二月活动回退，新增recorded_at_past锚点：原始用户报告近期已完成事件、无明示年份时，可按该来源记录日解析最近的不晚于记录日的日期列表。锚点只接受规定字面字符串，事件措辞须为连续原话；助手内容、派生事实、显式其他年份及计划不能靠此锚点建立已参加事实，实际完成与近期语义仍需复核。出席日的完整原文、分页和直接读取均执行所需计算，避免latest_state计划跳过校验。重建、463项测试和泄漏检查通过；先单题复查，再决定必要40题，v63仍使用其冻结源码完成评分。',
        '- v63完整评分35/40：跨会话14/16、知识更新7/8、时间推理7/8、用户事实3/3、助手事实2/2、偏好2/3。相对v62第二组修复出席日期、混合鱼数和月数精度，回退跨年活动、地点评分和电池建议，净增0题。所有40题缓存及BGE重排完整，Boolean评分完整、数字标签冲突0题。吉他答案与v62逐字相同却得到相反标签，另存评分波动审计；原始标签均保留，不重评相同答案。电池建议确实漏读了原文已有充电宝的拥有状态。',
        '- v64跨年开发题给出3天，并计算三个2023年十二月日期，来源、去重、算术、实际参加复核及自动评分全部通过，1/1。没有启动v64整批；合并另外两类读取修复后再验证，减少不必要的40题运行。',
        '- v65在原话索引及建议复核中按对象分别核对拥有状态：描述使用或整理自己的新物品可支持已拥有，另一对象的询问或计划不会抹掉该拥有状态；建议先沿用已有用品。完成动作的地点回忆先用完成报告的原始地点描述，早期考虑的商家名称须有身份关联。索引仍逐字保留角色、来源和计划措辞，超预算时仍省略索引。重建、463项测试及泄漏检查通过，先复查两道剩余回退。',
        '- v65开发复查2/2通过：电池建议正确识别已有充电宝并提出沿用办法，地点回答先给完成原文中的音乐店位置。与v64的跨年1/1合并后，单独确认成功退出码和完整正标签，再启动必要固定40题；开发复查不计作整批分数，也不改写先前负标签。',
        '- v65完整回答40题、全部缓存命中后，初次评分发生6条上游HTTP429端点不可用错误。已完成34条Boolean评分另存原始快照，只补评尚未评分的6题，未完成前不报整批分数。回答过程中观察到两处凭据格式失败：source_value误填时间短语，日期端点将来源中的今天改写成不在引用内的ISO日期；原拒答保留。',
        '- v66让转换错误明确指出source_value字段，并说明数值、单位及原始时间短语的不同位置；同单位时省略多余转换字段。相对日期说明要求使用来源原话的relative_text与记录日锚点，省略伪造date_text；错误反馈也指出这种合法修正。来源、算术、单位及日期校验均保持。新检查验证错误字段定位及相对日期修正，同时保持非法数值被拒绝。初次小预算测试失败记录保留，压缩日期示例后重建、465项测试与泄漏检查全部通过；先开发复查再决定整批。',
        '- v65补评完成，34条旧评分逐条与快照相同，最终35/40：跨会话14/16、知识更新8/8、时间推理5/8，其他题型全部通过。相对v63修复跨年活动、地点评分和已拥有用品的建议，回退时长合计格式、相对日期格式及上周六同行者，净增0题。同行者题已完成三页来源读取，最后调用遭遇HTTP429端点不可用，属于服务失败；原拒答和负标签保留。数字标签冲突0题，40条Boolean结果及缓存/BGE均完整。',
        '- v67合并v66字段修复，并给每次respond的读取模型单独包一层：仅对HTTP429且明确server_error/端点不可用的失败调用，等待30秒后最多追加一次同请求重试。成功输出、正常拒答、额度/鉴权及内容错误不重采样；共享模型和写入器不被替换，附加重试及错误写入trace。降低整批并发6→4以减少端点失败峰值，模型角色、配置和记忆库仍相同，所以评估包含代码与执行并发调整，不能单独归因于一个改动。重建、470项测试及泄漏检查通过；先复查v65的3个回退。v66在整批和开发复查启动前合并，未产生分数。',
        '- v67开发复查3/3通过：时长3.5周与阅读21天通过来源数值校验，同行者正确恢复为父母，原始评分全部为正。这次实际调用没有触发额外重试；策略由5项专门检查覆盖，不能把调用正常单独归因于该策略。单独确认完成退出码、缓存/BGE及Boolean正标签后，启动4并发固定40题。',
        '- v67完整40题为34/40（85%），跨会话13/16、更新7/8、时间6/8，其他类型全对；相对v65修复时长3.5周、阅读21天，但竞争性运动计数、烘焙次数、支持小组次数因持续接口故障回退。四道可回答错题包含最终接口故障，婚礼参考名称缺失与悬挂反馈/实际测试日期缺失仍为负标签；另有一条未答题故障经过两次续跑后完成，原39回答不变。评分先完成31题，之后只补9个未评分题，再补最后1题；所有已完成Boolean评分逐项不变。缓存40/BGE40/Boolean40、数字标签异常0，两条历史读取错误与嵌套接口故障均保留，不因剔除接口失败而调整完整分母。没有达到95%。',
        '- v67整批中实际出现多条延迟重试后仍失败的429，以及一条明确相同端点不可用消息的451；另有1题未产出答案，保留39个已完成回答，只续跑该未答题。v68依据这些已观察轨迹把每次失败读取的追加尝试上限改为3次，等待30/60/60秒；仅处理429或451且明确端点不可用，其他额度/鉴权/法律错误立即传播，成功回答或拒答不重采样。重建、473项测试与泄漏检查通过，固定同一模型、配置及写入库，并发再降到2；这包含执行并发调整，不能单独归因于代码。准备4个代表修复/校验/回答阶段的真实故障用例复查，后续发现的故障仍完整留在40题成绩中；前一冻结评测结束前不启动新的云端调用。报告新增嵌套的修复/校验接口故障统计，原始答案与评分仍计入完整分母。',
        '- v68开发复查4/4通过，竞技运动2项、烘焙4次、支持小组5次均恢复，原本无来源的居住题正常完成校验后拒答。缓存/BGE/来源校验均为4/4，实际触发9次读取附加重试，单次失败调用最高用到第3次追加尝试。初次评分2题失败，只对缺失标签使用同一冻结退避包装器、原固定评分模型与官方提示词补评；已完成2个Boolean标签原样保留，补评实际触发1次附加重试。辅助驱动器首次导入路径错误发生在云调用前，日志保留，修正后完成。完整退出与4个正标签单独确认后才启动固定40题；4题结果不替代整批成绩。',
        '- v68第二组固定开发样本完整39/40（97.5%）：跨会话16/16、更新8/8、时间7/8，其余全对；相对v67恢复四个接口故障错题，婚礼评分也转正，无回退。婚礼仍只回答来源支持的3场及新娘信息，没有补写缺失的参考名字；相关读取逻辑未改，原始生成与评分波动单独记录，不能把全部5个增益归因于退避策略。40题读取0接口错误，缓存/BGE/Boolean均40，数字异常0、未恢复读取接口失败0。评分首批28题保留原样，仅对12个缺失标签使用冻结退避、同一评分模型及官方提示词补评，实际追加3次重试。第一组固定回归已按相同源码/配置/2并发启动；两组均已用于开发，不是新独立或盲测。第一组完成且达标前不采用配置、不宣布整体目标完成。',
        '- v68第一组完整复测33/40（82.5%）：跨会话12/16、更新7/8、时间7/8、偏好2/3，其余全对；相对v62第一组38/40的五个回退均有最终端点故障。完整审计有110次附加读取重试，7个仍未恢复的嵌套接口失败；40题缓存/BGE/Boolean完整，数字异常0。首次36道回答保留原样，只补4道未答题；首批19个标签保留，只补21个缺失评分，实际再重试7次。原先两道负标签及全部接口拒答均保留，两个固定组同版39/40与33/40不能证明整体稳定达到95%，因此不采用v68。',
        '- v68第一组实际发现端点退避耗尽后，计算修复、来源复核或分页读取将接口故障吞为记忆不足的已完成答案。v69只让明确429/451端点不可用从这六处读取捕获路径继续抛出，交给现有评测续跑识别未完成题；不重新生成已完成答案，不改正常拒答、凭据错误、账户错误或任何评分标签，模型和有界退避保持相同。重建、477项测试及完整题库泄漏检查通过，写入/索引指纹保持不变。两组固定40题及两道实际故障开发复查均预先冻结，执行并发2→1也记录为对照条件变化；v68全组完成并保留原评分后，先启动小复查，通过后才决定整批。',
        '- v47是错题审计确认后启动的必要修复验证：仍是同一40题、同一v40记忆与32KB/64KB预算，修正日期结果格式、程序复制日期来源和明确时长求和；另用2个自拟案例检查实际写入和回答。相对v40仍包含此前v42读取提示词变化，不能单独归因于一个修复。',
        '- 用户进一步要求后续只抽约40题，未启动的v44整批100题已停止。v46–v48均使用按题型固定抽取的同一40题，未查看正确率或答案选择题目。v46与v40同时改变源码和预算，不能把分数差异归因于一个改动，也不能将40题结果当作100题结果。',
        '- v38直接提供全量历史，跳过抽取、检索及证据复核，89/100仅用于诊断模型和数据的限制，不是记忆系统准确率。',
        '- 同一模型用于回答及评分；评分标签不是人工裁定。精确字符串机制检查另行报告，不能替代题库评分。',
        '- UTF-8预算按字节计数，配置字段名称中的tokens不能解释为实际模型token；实际用量查看usage账本。价格未配置时费用未知，不能将账本中的零当作实际零费用。',
        '- 嵌入服务中断产生的失败日志保留；同一冻结版本只补跑未完成题，完整分母保持冻结样本的100题或40题。',
        '', '## 改动与来源', '',
        '跨页证据保留原始操作数和精确引文；说话人核对避免把助手的通用估价当成用户事实；历史状态辨认变更前一版本；完整原文在来源覆盖及预算都允许时直接合成；裁决输出截断保留新旧事实而不接受部分更新。', '',
        '下列来源及预算用于复现，完整逐题审计位于各运行目录的answers-and-audit.json。', '',
    ]
    for item in data:
        source = item['source'] or {}
        lines += [f"### {item['run']}", '',
                  f"源码：`{source.get('source_sha256')}`；写入源码：`{source.get('write_source_sha256')}`。",
                  f"配置：`{item['config_sha256']}`；样本：`{item['sample_sha256']}`。",
                  f"角色：`{json.dumps(item['roles'], ensure_ascii=False)}`。",
                  f"预算：`{json.dumps(item['memory_budgets'], ensure_ascii=False)}`。",
                  f"读取路径：`{json.dumps(item['trace_modes'])}`；历史调用失败记录：{item['recorded_errors']}，仍未答出的失败题：{item['unresolved_error_questions']}。", '']
        if item['mechanisms']:
            lines += [f"真实端到端机制检查：`{json.dumps(item['mechanisms'], ensure_ascii=False)}`。", '']
        if item['live_reader_check']:
            lines += [f"自拟真实模型读取检查（不含写入，不是题库分数）：`{json.dumps(item['live_reader_check'], ensure_ascii=False)}`。", '']
        if item['live_memory_subset']:
            lines += [f"开发子集复查（不是整批分数）：`{json.dumps(item['live_memory_subset'], ensure_ascii=False)}`。", '']
        if item['execution_status']:
            lines += [f"执行状态：`{item['execution_status']}`。", '']
        if item['paired'] and item['paired'].get('total', 0):
            pair = item['paired']
            lines += [f"配对：{pair['before']}→{pair['after']}；新增正确{len(pair['gained'])}题，回退{len(pair['lost'])}题。", '']
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text('\n'.join(lines) + '\n')
    audit_dir = args.runs / 'accuracy-campaign'
    audit_dir.mkdir(parents=True, exist_ok=True)
    (audit_dir / 'summary.json').write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    print(args.out)


if __name__ == '__main__':
    main()
