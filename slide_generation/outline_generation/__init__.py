# Outline Generation — Discourse parsing, slide planning, and integration
from .section_discourse_parser import (
    SectionDiscourseParserConfig,
    SectionDiscourseParser,
    split_into_sections,
    md_to_rst_text,
)

from .slide_planner import (
    SlidePlannerConfig,
    SlidePlanner,
)

from .narrative_critic import NarrativeCritic

from .slide_reviser import (
    SlideReviserConfig,
    SlideReviser,
)

from .summarization import (
    PaperSummarizerConfig,
    PaperSummarizer,
    run_summarization_pipeline,
)
