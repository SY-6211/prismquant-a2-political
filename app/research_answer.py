"""Research output contract. Premise links validate traceability, not economic truth."""
from typing import Literal, Annotated
from pydantic import Field, model_validator
from app.agent_tools import Arguments
from app.agent_analysis import Quote
Index = Annotated[int, Field(strict=True, ge=1)]
ShortText = Annotated[str, Field(min_length=1, max_length=300)]


class Fact(Arguments):
    text_zh: str = Field(min_length=1, max_length=300)
    citations: list[Quote] = Field(min_length=1, max_length=2)


class Inference(Arguments):
    text_zh: str = Field(min_length=1, max_length=250)
    premise_points: list[Index] = Field(min_length=1, max_length=4)
    mechanism_zh: str = Field(min_length=1, max_length=250)
    assumptions_zh: list[ShortText] = Field(min_length=1, max_length=3)
    counterfactors_zh: list[ShortText] = Field(min_length=1, max_length=3)


class Scenario(Arguments):
    condition_zh: str = Field(min_length=1, max_length=200)
    implication_zh: str = Field(min_length=1, max_length=200)
    analysis_indices: list[Index] = Field(min_length=1, max_length=3)


class ResearchAnswer(Arguments):
    outcome: Literal['answered', 'insufficient_evidence']
    points: list[Fact] = Field(max_length=4)
    analysis: list[Inference] = Field(max_length=3)
    scenarios: list[Scenario] = Field(max_length=3)
    evidence_gaps_zh: list[ShortText] = Field(min_length=1, max_length=5)
    conclusion_zh: str = Field(min_length=1, max_length=300)
    conclusion_basis: list[Index] = Field(max_length=3)
    limitations_zh: list[ShortText] = Field(min_length=1, max_length=4)
    needs_review: bool

    @model_validator(mode='after')
    def traceability(self):
        answered = self.outcome == 'answered'
        if answered != bool(self.points):
            raise ValueError('Research requires quoted premises or abstention')
        if answered and not (self.analysis and self.scenarios and self.conclusion_basis):
            raise ValueError('Research needs conditional analysis, scenarios and a bounded conclusion')
        if not answered and (self.analysis or self.scenarios or self.conclusion_basis):
            raise ValueError('No supported premises: do not fabricate research')
        def refs(values, count):
            if any(type(n) is not int or not 1 <= n <= count for n in values) or len(set(values)) != len(values):
                raise ValueError('Unknown or duplicate research premise')
        for item in self.analysis:
            refs(item.premise_points, len(self.points))
        for item in self.scenarios:
            refs(item.analysis_indices, len(self.analysis))
        refs(self.conclusion_basis, len(self.analysis))
        def strings(value):
            if isinstance(value, str):
                if not value.strip():
                    raise ValueError('Blank or oversized research text')
            elif isinstance(value, list):
                for row in value: strings(row)
            elif isinstance(value, dict):
                for row in value.values(): strings(row)
        strings(self.model_dump())
        return self


def empty_research():
    return dict(analysis=[], scenarios=[], evidence_gaps_zh=['缺少与问题直接相关的可用原文。'],
                conclusion_zh='现有证据不足，暂不能形成研究结论。', conclusion_basis=[])


class ResearchOutput(Arguments):
    """V2 output schema: the host derives outcome, so the model cannot contradict its own content."""
    points: list[Fact] = Field(max_length=4)
    analysis: list[Inference] = Field(max_length=3)
    scenarios: list[Scenario] = Field(max_length=3)
    evidence_gaps_zh: list[ShortText] = Field(min_length=1, max_length=5)
    conclusion_zh: str = Field(min_length=1, max_length=300)
    conclusion_basis: list[Index] = Field(max_length=3)
    limitations_zh: list[ShortText] = Field(min_length=1, max_length=4)
    needs_review: bool

    @property
    def outcome(self):
        return 'answered' if self.points else 'insufficient_evidence'

    @model_validator(mode='after')
    def traceability(self):
        # Same quote-premise/scenario/conclusion constraints as the historical contract.
        return ResearchAnswer.traceability(self)


class ResearchStep(Arguments):
    data_zh: ShortText
    purpose_zh: ShortText
    how_to_check_zh: ShortText
    basis_type: Literal['source_linked','general_method']
    premise_points: list[Index] = Field(max_length=4)


class ResearchResponse(ResearchOutput):
    """V3 output adds labeled methods; current company facts still require source evidence."""
    research_steps: list[ResearchStep] = Field(max_length=6)

    @property
    def outcome(self):return 'answered' if self.points or self.research_steps else 'insufficient_evidence'

    @model_validator(mode='after')
    def traceability(self):
        if not self.research_steps:return ResearchAnswer.traceability(self)
        if self.analysis:ResearchAnswer.traceability(self)
        elif self.scenarios or self.conclusion_basis:raise ValueError('Scenarios need cited analysis')
        for step in self.research_steps:
            if step.basis_type=='source_linked':
                if not step.premise_points:raise ValueError('Source-linked method needs quoted premises')
                if any(n>len(self.points) for n in step.premise_points):raise ValueError('Unknown method premise')
            elif step.premise_points:raise ValueError('General methods cannot borrow source attribution')
            if len(set(step.premise_points))!=len(step.premise_points):raise ValueError('Duplicate method premise')
        def check(v):
            if isinstance(v,str) and not v.strip():raise ValueError('Blank research text')
            if isinstance(v,dict):
                for item in v.values():check(item)
            if isinstance(v,list):
                for item in v:check(item)
        check(self.model_dump());return self


def narrative(value):
    parts = ['原文事实\n' + '\n'.join(p['text_zh'] for p in value['points'])] if value['points'] else ['本次未取得可引用的人物原文；以下仅为一般研究方法。']
    for i, item in enumerate(value['analysis'], 1):
        parts.append(f"条件分析 {i}：{item['text_zh']}\n传导机制：{item['mechanism_zh']}\n"
                     + '假设：' + '；'.join(item['assumptions_zh']) + '\n反向因素：' + '；'.join(item['counterfactors_zh']))
    for item in value['scenarios']:
        parts.append('情景：' + item['condition_zh'] + '\n可能影响：' + item['implication_zh'])
    for item in value.get('research_steps',[]):
        parts.append('待核对资料：'+item['data_zh']+'\n用途：'+item['purpose_zh']+'\n核对方式：'+item['how_to_check_zh']+'\n依据类型：'+('原文相关研究方向，数据尚未核验' if item['basis_type']=='source_linked' else '一般研究方法，非人物原话或已核验事实'))
    parts.extend(['还需核对：' + '；'.join(value['evidence_gaps_zh']), '研究结论：' + value['conclusion_zh']])
    return '\n\n'.join(parts)
