"""OASF skill taxonomy — sourced from the ADS instance's own taxonomy browser.

Each entry:
  name  — human-readable label (shown in the UI)
  id    — integer taxonomy ID sent to ADS routing
  key   — canonical path-style identifier used as card skill.id and routing skill name
"""

from __future__ import annotations

OASF_SKILLS: list[dict] = [
    # ── Natural Language Processing ───────────────────────────────────────────
    # Natural Language Understanding [101]
    {"name": "NLP / Natural Language Understanding / Contextual Comprehension", "id": 10101, "key": "nlp/natural_language_understanding/contextual_comprehension"},
    {"name": "NLP / Natural Language Understanding / Semantic Understanding", "id": 10102, "key": "nlp/natural_language_understanding/semantic_understanding"},
    {"name": "NLP / Natural Language Understanding / Entity Recognition", "id": 10103, "key": "nlp/natural_language_understanding/entity_recognition"},
    # Natural Language Generation [102]
    {"name": "NLP / Natural Language Generation / Text Completion", "id": 10201, "key": "nlp/natural_language_generation/text_completion"},
    {"name": "NLP / Natural Language Generation / Text Summarization", "id": 10202, "key": "nlp/natural_language_generation/text_summarization"},
    {"name": "NLP / Natural Language Generation / Text Paraphrasing", "id": 10203, "key": "nlp/natural_language_generation/text_paraphrasing"},
    {"name": "NLP / Natural Language Generation / Dialogue Generation", "id": 10204, "key": "nlp/natural_language_generation/dialogue_generation"},
    {"name": "NLP / Natural Language Generation / Question Generation", "id": 10205, "key": "nlp/natural_language_generation/question_generation"},
    {"name": "NLP / Natural Language Generation / Text Style Transfer", "id": 10206, "key": "nlp/natural_language_generation/text_style_transfer"},
    {"name": "NLP / Natural Language Generation / Story Generation", "id": 10207, "key": "nlp/natural_language_generation/story_generation"},
    # Information Retrieval and Synthesis [103]
    {"name": "NLP / Information Retrieval & Synthesis / Fact Extraction", "id": 10301, "key": "nlp/information_retrieval_and_synthesis/fact_extraction"},
    {"name": "NLP / Information Retrieval & Synthesis / Question Answering", "id": 10302, "key": "nlp/information_retrieval_and_synthesis/question_answering"},
    {"name": "NLP / Information Retrieval & Synthesis / Knowledge Synthesis", "id": 10303, "key": "nlp/information_retrieval_and_synthesis/knowledge_synthesis"},
    {"name": "NLP / Information Retrieval & Synthesis / Sentence Similarity", "id": 10304, "key": "nlp/information_retrieval_and_synthesis/sentence_similarity"},
    {"name": "NLP / Information Retrieval & Synthesis / Document and Passage Retrieval", "id": 10305, "key": "nlp/information_retrieval_and_synthesis/document_and_passage_retrieval"},
    {"name": "NLP / Information Retrieval & Synthesis / Search", "id": 10306, "key": "nlp/information_retrieval_and_synthesis/search"},
    # Creative Content Generation [104]
    {"name": "NLP / Creative Content Generation / Storytelling", "id": 10401, "key": "nlp/creative_content_generation/storytelling"},
    {"name": "NLP / Creative Content Generation / Poetry and Creative Writing", "id": 10402, "key": "nlp/creative_content_generation/poetry_and_creative_writing"},
    # Language Translation and Multilingual Support [105]
    {"name": "NLP / Language Translation / Translation", "id": 10501, "key": "nlp/language_translation/translation"},
    {"name": "NLP / Language Translation / Multilingual Understanding", "id": 10502, "key": "nlp/language_translation/multilingual_understanding"},
    # Personalisation and Adaptation [106]
    {"name": "NLP / Personalisation & Adaptation / User Adaptation", "id": 10601, "key": "nlp/personalisation_and_adaptation/user_adaptation"},
    {"name": "NLP / Personalisation & Adaptation / Tone and Style Adjustment", "id": 10602, "key": "nlp/personalisation_and_adaptation/tone_and_style_adjustment"},
    # Analytical and Logical Reasoning [107]
    {"name": "NLP / Analytical & Logical Reasoning / Inference and Deduction", "id": 10701, "key": "nlp/analytical_and_logical_reasoning/inference_and_deduction"},
    {"name": "NLP / Analytical & Logical Reasoning / Problem Solving", "id": 10702, "key": "nlp/analytical_and_logical_reasoning/problem_solving"},
    {"name": "NLP / Analytical & Logical Reasoning / Fact and Claim Verification", "id": 10703, "key": "nlp/analytical_and_logical_reasoning/fact_and_claim_verification"},
    # Ethical and Safe Interaction [108]
    {"name": "NLP / Ethical & Safe Interaction / Bias Mitigation", "id": 10801, "key": "nlp/ethical_and_safe_interaction/bias_mitigation"},
    {"name": "NLP / Ethical & Safe Interaction / Content Moderation", "id": 10802, "key": "nlp/ethical_and_safe_interaction/content_moderation"},
    # Text Classification [109]
    {"name": "NLP / Text Classification / Topic Labelling and Tagging", "id": 10901, "key": "nlp/text_classification/topic_labelling_and_tagging"},
    {"name": "NLP / Text Classification / Sentiment Analysis", "id": 10902, "key": "nlp/text_classification/sentiment_analysis"},
    {"name": "NLP / Text Classification / Natural Language Inference", "id": 10903, "key": "nlp/text_classification/natural_language_inference"},
    # Feature Extraction [110]
    {"name": "NLP / Feature Extraction / Model Feature Extraction", "id": 11001, "key": "nlp/feature_extraction/model_feature_extraction"},
    # Token Classification [111]
    {"name": "NLP / Token Classification / Named Entity Recognition", "id": 11101, "key": "nlp/token_classification/named_entity_recognition"},
    {"name": "NLP / Token Classification / Part-of-Speech Tagging", "id": 11102, "key": "nlp/token_classification/part_of_speech_tagging"},

    # ── Images / Computer Vision ──────────────────────────────────────────────
    {"name": "Images & Computer Vision / Image Segmentation", "id": 201, "key": "images_and_computer_vision/image_segmentation"},
    {"name": "Images & Computer Vision / Video Classification", "id": 202, "key": "images_and_computer_vision/video_classification"},
    {"name": "Images & Computer Vision / Image Classification", "id": 203, "key": "images_and_computer_vision/image_classification"},
    {"name": "Images & Computer Vision / Object Detection", "id": 204, "key": "images_and_computer_vision/object_detection"},
    {"name": "Images & Computer Vision / Keypoint Detection", "id": 205, "key": "images_and_computer_vision/keypoint_detection"},
    {"name": "Images & Computer Vision / Image Generation", "id": 206, "key": "images_and_computer_vision/image_generation"},
    {"name": "Images & Computer Vision / Depth Estimation", "id": 207, "key": "images_and_computer_vision/depth_estimation"},
    {"name": "Images & Computer Vision / Image Feature Extraction", "id": 208, "key": "images_and_computer_vision/image_feature_extraction"},
    {"name": "Images & Computer Vision / Mask Generation", "id": 209, "key": "images_and_computer_vision/mask_generation"},
    {"name": "Images & Computer Vision / Image-to-Image", "id": 210, "key": "images_and_computer_vision/image_to_image"},
    {"name": "Images & Computer Vision / Image-to-3D", "id": 211, "key": "images_and_computer_vision/image_to_3d"},

    # ── Audio ─────────────────────────────────────────────────────────────────
    {"name": "Audio / Audio Classification", "id": 301, "key": "audio/audio_classification"},
    {"name": "Audio / Audio to Audio", "id": 302, "key": "audio/audio_to_audio"},

    # ── Tabular / Text ────────────────────────────────────────────────────────
    {"name": "Tabular & Text / Tabular Classification", "id": 401, "key": "tabular_and_text/tabular_classification"},
    {"name": "Tabular & Text / Tabular Regression", "id": 402, "key": "tabular_and_text/tabular_regression"},

    # ── Analytical Skills ─────────────────────────────────────────────────────
    # Mathematical Reasoning [501]
    {"name": "Analytical Skills / Mathematical Reasoning / Pure Mathematical Operations", "id": 50101, "key": "analytical_skills/mathematical_reasoning/pure_mathematical_operations"},
    {"name": "Analytical Skills / Mathematical Reasoning / Math Word Problems", "id": 50102, "key": "analytical_skills/mathematical_reasoning/math_word_problems"},
    {"name": "Analytical Skills / Mathematical Reasoning / Geometry", "id": 50103, "key": "analytical_skills/mathematical_reasoning/geometry"},
    {"name": "Analytical Skills / Mathematical Reasoning / Automated Theorem Proving", "id": 50104, "key": "analytical_skills/mathematical_reasoning/automated_theorem_proving"},
    # Coding Skills [502]
    {"name": "Analytical Skills / Coding Skills / Text to Code", "id": 50201, "key": "analytical_skills/coding_skills/text_to_code"},
    {"name": "Analytical Skills / Coding Skills / Code to Docstrings", "id": 50202, "key": "analytical_skills/coding_skills/code_to_docstrings"},
    {"name": "Analytical Skills / Coding Skills / Code Template Filling", "id": 50203, "key": "analytical_skills/coding_skills/code_template_filling"},
    {"name": "Analytical Skills / Coding Skills / Code Refactoring and Optimization", "id": 50204, "key": "analytical_skills/coding_skills/code_refactoring_and_optimization"},

    # ── Retrieval Augmented Generation ────────────────────────────────────────
    {"name": "RAG / Retrieval of Information / Indexing", "id": 60101, "key": "rag/retrieval_of_information/indexing"},
    {"name": "RAG / Retrieval of Information / Search", "id": 60102, "key": "rag/retrieval_of_information/search"},
    {"name": "RAG / Retrieval of Information / Document Retrieval", "id": 60103, "key": "rag/retrieval_of_information/document_retrieval"},
    {"name": "RAG / Document or Database Question Answering", "id": 602, "key": "rag/document_or_database_question_answering"},
    {"name": "RAG / Generation of Any", "id": 603, "key": "rag/generation_of_any"},

    # ── Multi-modal ───────────────────────────────────────────────────────────
    {"name": "Multimodal / Image Processing / Image to Text", "id": 70101, "key": "multimodal/image_processing/image_to_text"},
    {"name": "Multimodal / Image Processing / Text to Image", "id": 70102, "key": "multimodal/image_processing/text_to_image"},
    {"name": "Multimodal / Image Processing / Text to Video", "id": 70103, "key": "multimodal/image_processing/text_to_video"},
    {"name": "Multimodal / Image Processing / Text to 3D", "id": 70104, "key": "multimodal/image_processing/text_to_3d"},
    {"name": "Multimodal / Image Processing / Visual Question Answering", "id": 70105, "key": "multimodal/image_processing/visual_question_answering"},
    {"name": "Multimodal / Audio Processing / Text to Speech", "id": 70201, "key": "multimodal/audio_processing/text_to_speech"},
    {"name": "Multimodal / Audio Processing / Automatic Speech Recognition", "id": 70202, "key": "multimodal/audio_processing/automatic_speech_recognition"},
    {"name": "Multimodal / Any to Any Transformation", "id": 703, "key": "multimodal/any_to_any_transformation"},

    # ── Security & Privacy ────────────────────────────────────────────────────
    {"name": "Security & Privacy / Threat Detection", "id": 801, "key": "security_and_privacy/threat_detection"},
    {"name": "Security & Privacy / Vulnerability Analysis", "id": 802, "key": "security_and_privacy/vulnerability_analysis"},
    {"name": "Security & Privacy / Secret Leak Detection", "id": 803, "key": "security_and_privacy/secret_leak_detection"},
    {"name": "Security & Privacy / Privacy Risk Assessment", "id": 804, "key": "security_and_privacy/privacy_risk_assessment"},

    # ── Data Engineering ──────────────────────────────────────────────────────
    {"name": "Data Engineering / Data Cleaning", "id": 901, "key": "data_engineering/data_cleaning"},
    {"name": "Data Engineering / Schema Inference", "id": 902, "key": "data_engineering/schema_inference"},
    {"name": "Data Engineering / Feature Engineering", "id": 903, "key": "data_engineering/feature_engineering"},
    {"name": "Data Engineering / Data Transformation Pipeline", "id": 904, "key": "data_engineering/data_transformation_pipeline"},
    {"name": "Data Engineering / Data Quality Assessment", "id": 905, "key": "data_engineering/data_quality_assessment"},

    # ── Agent Orchestration ───────────────────────────────────────────────────
    {"name": "Agent Orchestration / Task Decomposition", "id": 1001, "key": "agent_orchestration/task_decomposition"},
    {"name": "Agent Orchestration / Role Assignment", "id": 1002, "key": "agent_orchestration/role_assignment"},
    {"name": "Agent Orchestration / Multi-Agent Planning", "id": 1003, "key": "agent_orchestration/multi_agent_planning"},
    {"name": "Agent Orchestration / Agent Coordination", "id": 1004, "key": "agent_orchestration/agent_coordination"},
    {"name": "Agent Orchestration / Negotiation and Resolution", "id": 1005, "key": "agent_orchestration/negotiation_and_resolution"},

    # ── Evaluation & Monitoring ───────────────────────────────────────────────
    {"name": "Evaluation & Monitoring / Benchmark Execution", "id": 1101, "key": "evaluation_and_monitoring/benchmark_execution"},
    {"name": "Evaluation & Monitoring / Test Case Generation", "id": 1102, "key": "evaluation_and_monitoring/test_case_generation"},
    {"name": "Evaluation & Monitoring / Quality Evaluation", "id": 1103, "key": "evaluation_and_monitoring/quality_evaluation"},
    {"name": "Evaluation & Monitoring / Anomaly Detection", "id": 1104, "key": "evaluation_and_monitoring/anomaly_detection"},
    {"name": "Evaluation & Monitoring / Performance Monitoring", "id": 1105, "key": "evaluation_and_monitoring/performance_monitoring"},

    # ── DevOps / MLOps ────────────────────────────────────────────────────────
    {"name": "DevOps & MLOps / Infrastructure Provisioning", "id": 1201, "key": "devops_and_mlops/infrastructure_provisioning"},
    {"name": "DevOps & MLOps / Deployment Orchestration", "id": 1202, "key": "devops_and_mlops/deployment_orchestration"},
    {"name": "DevOps & MLOps / CI/CD Configuration", "id": 1203, "key": "devops_and_mlops/ci_cd_configuration"},
    {"name": "DevOps & MLOps / Model Versioning", "id": 1204, "key": "devops_and_mlops/model_versioning"},
    {"name": "DevOps & MLOps / Monitoring & Alerting", "id": 1205, "key": "devops_and_mlops/monitoring_and_alerting"},

    # ── Governance & Compliance ───────────────────────────────────────────────
    {"name": "Governance & Compliance / Policy Mapping", "id": 1301, "key": "governance_and_compliance/policy_mapping"},
    {"name": "Governance & Compliance / Compliance Assessment", "id": 1302, "key": "governance_and_compliance/compliance_assessment"},
    {"name": "Governance & Compliance / Audit Trail Summarization", "id": 1303, "key": "governance_and_compliance/audit_trail_summarization"},
    {"name": "Governance & Compliance / Risk Classification", "id": 1304, "key": "governance_and_compliance/risk_classification"},

    # ── Tool Interaction ──────────────────────────────────────────────────────
    {"name": "Tool Interaction / API Schema Understanding", "id": 1401, "key": "tool_interaction/api_schema_understanding"},
    {"name": "Tool Interaction / Workflow Automation", "id": 1402, "key": "tool_interaction/workflow_automation"},
    {"name": "Tool Interaction / Tool Use Planning", "id": 1403, "key": "tool_interaction/tool_use_planning"},
    {"name": "Tool Interaction / Script Integration", "id": 1404, "key": "tool_interaction/script_integration"},

    # ── Advanced Reasoning & Planning ─────────────────────────────────────────
    {"name": "Advanced Reasoning & Planning / Strategic Planning", "id": 1501, "key": "advanced_reasoning_and_planning/strategic_planning"},
    {"name": "Advanced Reasoning & Planning / Long-Horizon Reasoning", "id": 1502, "key": "advanced_reasoning_and_planning/long_horizon_reasoning"},
    {"name": "Advanced Reasoning & Planning / Chain-of-Thought Structuring", "id": 1503, "key": "advanced_reasoning_and_planning/chain_of_thought_structuring"},
    {"name": "Advanced Reasoning & Planning / Hypothesis Generation", "id": 1504, "key": "advanced_reasoning_and_planning/hypothesis_generation"},
]
