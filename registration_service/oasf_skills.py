"""Hardcoded OASF skill taxonomy — sourced from agntcy/oasf schema/skills/.

Each entry: {"name": <taxonomy path>, "id": <int>}
IDs follow the pattern: parent_category_id * 100 + skill uid.
"""

from __future__ import annotations

OASF_SKILLS: list[dict] = [
    # ── Natural Language Processing (100) ─────────────────────────────────────
    # natural_language_generation (102)
    {"name": "natural_language_processing/natural_language_generation/translation", "id": 10201},
    {"name": "natural_language_processing/natural_language_generation/summarization", "id": 10202},
    {"name": "natural_language_processing/natural_language_generation/text_generation", "id": 10203},
    {"name": "natural_language_processing/natural_language_generation/paraphrasing", "id": 10204},
    {"name": "natural_language_processing/natural_language_generation/story_generation", "id": 10205},
    {"name": "natural_language_processing/natural_language_generation/dialogue_generation", "id": 10206},
    # natural_language_understanding (101)
    {"name": "natural_language_processing/natural_language_understanding/sentiment_analysis", "id": 10101},
    {"name": "natural_language_processing/natural_language_understanding/named_entity_recognition", "id": 10102},
    {"name": "natural_language_processing/natural_language_understanding/intent_detection", "id": 10103},
    {"name": "natural_language_processing/natural_language_understanding/text_classification", "id": 10104},
    {"name": "natural_language_processing/natural_language_understanding/relation_extraction", "id": 10105},
    {"name": "natural_language_processing/natural_language_understanding/coreference_resolution", "id": 10106},
    # information_retrieval_synthesis (103)
    {"name": "natural_language_processing/information_retrieval_synthesis/document_search", "id": 10301},
    {"name": "natural_language_processing/information_retrieval_synthesis/question_answering", "id": 10302},
    {"name": "natural_language_processing/information_retrieval_synthesis/knowledge_synthesis", "id": 10303},
    {"name": "natural_language_processing/information_retrieval_synthesis/fact_checking", "id": 10304},
    {"name": "natural_language_processing/information_retrieval_synthesis/information_extraction", "id": 10305},
    {"name": "natural_language_processing/information_retrieval_synthesis/information_retrieval_synthesis_search", "id": 10306},
    # conversational (104)
    {"name": "natural_language_processing/conversational/task_oriented_dialogue", "id": 10401},
    {"name": "natural_language_processing/conversational/open_domain_dialogue", "id": 10402},
    {"name": "natural_language_processing/conversational/customer_support", "id": 10403},

    # ── Images / Computer Vision (200) ────────────────────────────────────────
    {"name": "images_computer_vision/image_captioning", "id": 201},
    {"name": "images_computer_vision/visual_question_answering", "id": 202},
    {"name": "images_computer_vision/image_classification", "id": 203},
    {"name": "images_computer_vision/object_detection", "id": 204},
    {"name": "images_computer_vision/image_segmentation", "id": 205},
    {"name": "images_computer_vision/optical_character_recognition", "id": 206},
    {"name": "images_computer_vision/image_generation", "id": 207},
    {"name": "images_computer_vision/image_editing", "id": 208},
    {"name": "images_computer_vision/face_recognition", "id": 209},
    {"name": "images_computer_vision/scene_understanding", "id": 210},
    {"name": "images_computer_vision/geolocation", "id": 211},

    # ── Audio / Speech (300) ──────────────────────────────────────────────────
    {"name": "audio_speech/speech_recognition", "id": 301},
    {"name": "audio_speech/text_to_speech", "id": 302},
    {"name": "audio_speech/speaker_identification", "id": 303},
    {"name": "audio_speech/audio_classification", "id": 304},
    {"name": "audio_speech/speech_translation", "id": 305},

    # ── Multimodal (400) ──────────────────────────────────────────────────────
    {"name": "multimodal/visual_text_understanding", "id": 401},
    {"name": "multimodal/document_understanding", "id": 402},
    {"name": "multimodal/cross_modal_retrieval", "id": 403},
    {"name": "multimodal/multimodal_generation", "id": 404},

    # ── Analytical Skills (500) ───────────────────────────────────────────────
    # coding_skills (502)
    {"name": "analytical_skills/coding_skills/text_to_code", "id": 50201},
    {"name": "analytical_skills/coding_skills/code_review", "id": 50202},
    {"name": "analytical_skills/coding_skills/code_debugging", "id": 50203},
    {"name": "analytical_skills/coding_skills/code_optimization", "id": 50204},
    {"name": "analytical_skills/coding_skills/code_documentation", "id": 50205},
    {"name": "analytical_skills/coding_skills/test_generation", "id": 50206},
    {"name": "analytical_skills/coding_skills/code_translation", "id": 50207},
    # data_analysis (501)
    {"name": "analytical_skills/data_analysis/data_visualization", "id": 50101},
    {"name": "analytical_skills/data_analysis/statistical_analysis", "id": 50102},
    {"name": "analytical_skills/data_analysis/anomaly_detection", "id": 50103},
    {"name": "analytical_skills/data_analysis/forecasting", "id": 50104},
    {"name": "analytical_skills/data_analysis/data_cleaning", "id": 50105},
    # math_reasoning (503)
    {"name": "analytical_skills/math_reasoning/arithmetic", "id": 50301},
    {"name": "analytical_skills/math_reasoning/symbolic_reasoning", "id": 50302},
    {"name": "analytical_skills/math_reasoning/theorem_proving", "id": 50303},

    # ── Retrieval Augmented Generation (600) ──────────────────────────────────
    # retrieval_of_information (601)
    {"name": "retrieval_augmented_generation/retrieval_of_information/web_retrieval", "id": 60101},
    {"name": "retrieval_augmented_generation/retrieval_of_information/knowledge_base_retrieval", "id": 60102},
    {"name": "retrieval_augmented_generation/retrieval_of_information/document_retrieval", "id": 60103},
    {"name": "retrieval_augmented_generation/retrieval_of_information/semantic_search", "id": 60104},
    # generation (602)
    {"name": "retrieval_augmented_generation/generation/grounded_generation", "id": 60201},
    {"name": "retrieval_augmented_generation/generation/citation_generation", "id": 60202},

    # ── Advanced Reasoning & Planning (1500) ──────────────────────────────────
    {"name": "advanced_reasoning_planning/strategic_planning", "id": 1501},
    {"name": "advanced_reasoning_planning/task_decomposition", "id": 1502},
    {"name": "advanced_reasoning_planning/causal_reasoning", "id": 1503},
    {"name": "advanced_reasoning_planning/commonsense_reasoning", "id": 1504},
    {"name": "advanced_reasoning_planning/multi_step_reasoning", "id": 1505},
    {"name": "advanced_reasoning_planning/decision_making", "id": 1506},
    {"name": "advanced_reasoning_planning/hypothesis_generation", "id": 1507},

    # ── Actions / Tool Use (700) ──────────────────────────────────────────────
    {"name": "actions_tool_use/web_browsing", "id": 701},
    {"name": "actions_tool_use/api_calling", "id": 702},
    {"name": "actions_tool_use/file_management", "id": 703},
    {"name": "actions_tool_use/calendar_management", "id": 704},
    {"name": "actions_tool_use/email_management", "id": 705},
    {"name": "actions_tool_use/database_querying", "id": 706},
    {"name": "actions_tool_use/computer_use", "id": 707},

    # ── Domain-Specific (800) ─────────────────────────────────────────────────
    {"name": "domain_specific/medical_clinical", "id": 801},
    {"name": "domain_specific/legal", "id": 802},
    {"name": "domain_specific/financial", "id": 803},
    {"name": "domain_specific/scientific_research", "id": 804},
    {"name": "domain_specific/geospatial", "id": 805},
    {"name": "domain_specific/cybersecurity", "id": 806},
    {"name": "domain_specific/education", "id": 807},
    {"name": "domain_specific/customer_service", "id": 808},
    {"name": "domain_specific/human_resources", "id": 809},
    {"name": "domain_specific/supply_chain", "id": 810},

    # ── Safety & Alignment (900) ──────────────────────────────────────────────
    {"name": "safety_alignment/content_moderation", "id": 901},
    {"name": "safety_alignment/bias_detection", "id": 902},
    {"name": "safety_alignment/toxicity_detection", "id": 903},
    {"name": "safety_alignment/pii_detection", "id": 904},
]

# Quick lookup by taxonomy path
OASF_SKILLS_BY_NAME: dict[str, dict] = {s["name"]: s for s in OASF_SKILLS}
