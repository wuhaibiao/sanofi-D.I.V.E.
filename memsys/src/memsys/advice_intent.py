"""Bounded cause-assessment instructions, separate from ordinary recommendations."""
import re


def procedural_advice(question):
    """Recognize forward-looking help requests, not recollection of old steps."""
    if re.search(r'\b(?:remember|recall)\b|\b(?:what|which|how)\s+(?:did|was|were)\b|'
                 r'(?:记得|回忆)|(?:之前|上次).{0,8}(?:你给|你推荐|你建议)', question, re.I):
        return False
    return bool(re.search(
        r'\bhow (?:can|could|should|would) (?:i|we)\b[^?\n]*'
        r'\b(?:improve|better|start|prepare|plan|organize|organise|practice|practise|approach|manage|learn|make)\b|'
        r'\b(?:what|which)\s+(?:(?:would|could)\s+be\s+)?(?:a\s+)?(?:sensible\s+|practical\s+)?'
        r'(?:first|next|initial)\s+(?:steps?|actions?)\b|'
        r'\b(?:can|could|would|will) you\b[^?\n]*\b(?:plan|steps|approach|strategy|improve|help)\b|'
        r'(?:怎么|怎样|如何).{0,24}(?:改进|改善|提高|提升|开始|着手|准备|安排|制定|规划|做得更好|练习|制作)|'
        r'(?:给|帮).{0,8}(?:我|我们).{0,12}(?:计划|方案|步骤|思路)', question, re.I))

_CAUSE_QUERY=re.compile(
    r"\bdo you think (?:it|this|that) (?:might|could|may)\b|"
    r"\b(?:what|which) (?:might|could|may) (?:be )?(?:caus(?:e|ing)|explain)\b|"
    r"\b(?:might|could|may) this (?:be why|explain)\b|"
    r"你觉得.*(?:可能|会不会)|(?:可能|会不会).*原因|什么(?:可能)?导致",re.I)

CAUSE_ANALYSIS_RULES=(
    "This is a possible-cause assessment, not only a request for tips. First explain plausible mechanisms "
    "tied to the user's explicitly recalled circumstances and current self-report. Distinguish confirmed "
    "personal facts from conditional hypotheses; do not claim a diagnosis or a certain cause. "
    "Consider timing and recent changes. If a relevant change is unconfirmed, ask one focused question "
    "about it or frame it as an if-condition, never as something the user already did. Old assistant "
    "suggestions do not prove the user performed them. Then give concise practical next steps. "
    "A list of generic remedies alone does not answer why the remembered situation might contribute.")


def causal_advice_rules(question,operation):
    return CAUSE_ANALYSIS_RULES if operation=='advice' and _CAUSE_QUERY.search(question) else ''
