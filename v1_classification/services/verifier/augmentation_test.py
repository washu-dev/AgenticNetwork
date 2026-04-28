# Copyright AGNTCY Contributors
# SPDX-License-Identifier: Apache-2.0

import logging

from shared.schemas import (
    ClassificationResult,
    VerificationRecommendation,
    VerificationTest,
    VerificationTestResult,
)

logger = logging.getLogger(__name__)


class AugmentationStabilityTester:
    """Placeholder augmentation-stability verifier.

    The planner currently calls ``Verifier.verify`` without the optional
    agent/slim arguments needed to run image augmentations. Keeping this class
    present lets the verifier initialize and returns a skip result if the
    optional path is enabled later without a concrete implementation.
    """

    def __init__(self, stability_threshold: float = 0.67):
        self.stability_threshold = stability_threshold

    async def verify(
        self,
        result: ClassificationResult,
        agent_url: str,
        image_url: str,
        slim_client: object,
    ) -> VerificationTest:
        logger.info(
            "Augmentation stability test skipped for %s; no augmentation runner is configured",
            result.request_id,
        )
        return VerificationTest(
            test_name="augmentation_stability",
            result=VerificationTestResult.SKIP,
            details={
                "threshold": self.stability_threshold,
                "agent_url": agent_url,
                "image_url": image_url,
                "recommendation": VerificationRecommendation.ACCEPT,
                "reason": "Augmentation stability test is not implemented for this runtime path.",
            },
        )
