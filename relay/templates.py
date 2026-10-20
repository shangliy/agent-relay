"""Six task-category templates (section 03).

These are text templates with a fixed deliverable contract — not full
workflows. Tool-using pipelines (search, rendering, patches) are later phases.
"""

from __future__ import annotations

TEMPLATE_VERSION = "2026-10-19.1"

_RESEARCH_SCHEMA = {
    "type": "object",
    "required": ["summary", "evidence", "disagreements", "limitations"],
    "properties": {
        "summary": {"type": "string", "minLength": 1},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["claim", "source"],
                "properties": {"claim": {"type": "string"}, "source": {"type": "string"}},
            },
        },
        "disagreements": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
}

_IDEA_SCHEMA = {
    "type": "object",
    "required": ["ideas"],
    "properties": {
        "ideas": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["title", "hypothesis", "baselines", "minimal_experiment"],
                "properties": {
                    "title": {"type": "string"},
                    "hypothesis": {"type": "string"},
                    "baselines": {"type": "array", "items": {"type": "string"}},
                    "minimal_experiment": {"type": "string"},
                },
            },
        }
    },
}

TEMPLATES: dict[str, dict] = {
    "video": {
        "label": "Video 策划包",
        "prompt": "为以下主题制作短视频策划包：选题角度、分镜表（镜头/画面/时长）、口播稿、字幕文本。只使用提供的材料，不虚构数据。",
        "deliverable": "Markdown：选题、分镜表、口播稿、字幕。",
        "validator": {"id": "min_words", "params": {"min": 200}},
    },
    "blog": {
        "label": "Blog Post",
        "prompt": "根据提供的公开材料撰写博客文章，包含标题、正文、摘要，以及一份需要人工核实的事实清单。",
        "deliverable": "Markdown：标题、摘要、正文、待核实清单。",
        "validator": {"id": "min_words", "params": {"min": 300}},
    },
    "research": {
        "label": "Research 报告",
        "prompt": "仅基于提供的材料撰写研究简报。每条结论标注材料来源；列出材料之间的分歧和本报告的局限。",
        "deliverable": "JSON 研究报告。",
        "validator": {"id": "json_schema", "params": {"schema": _RESEARCH_SCHEMA}},
        "deliverable_schema": _RESEARCH_SCHEMA,
    },
    "paper_idea": {
        "label": "Paper Idea",
        "prompt": "基于提供的材料提出候选研究想法。每个想法给出可证伪假设、基线和最小实验。不要声称新颖性已被证明。",
        "deliverable": "JSON 想法列表。",
        "validator": {"id": "json_schema", "params": {"schema": _IDEA_SCHEMA}},
        "deliverable_schema": _IDEA_SCHEMA,
    },
    "paper_review": {
        "label": "Paper Review（仅公开论文）",
        "prompt": "对提供的公开论文做结构化评审：贡献、方法、实验、问题与证据位置（章节/图表编号）。不得用于保密审稿材料。",
        "deliverable": "Markdown：摘要、优点、问题（附证据位置）、建议。",
        "validator": {"id": "min_words", "params": {"min": 300}},
    },
    "product": {
        "label": "产品优化方案",
        "prompt": "根据提供的产品描述与数据，输出诊断、优先级排序、实验方案和衡量指标。只输出方案，不修改任何代码或生产环境。",
        "deliverable": "Markdown：诊断、优先级、实验方案、指标。",
        "validator": {"id": "min_words", "params": {"min": 200}},
    },
}


def public_templates() -> list[dict]:
    return [{"category": k, "template_version": TEMPLATE_VERSION, **v} for k, v in TEMPLATES.items()]
