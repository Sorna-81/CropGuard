"""Normalized image prediction providers for disease and pest models."""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Protocol

import requests


@dataclass(frozen=True)
class Prediction:
    class_name: str
    confidence: float
    model_name: str
    model_version: str
    prediction_timestamp: str


class DiseasePredictionProvider(Protocol):
    def predict(self, image: bytes) -> Optional[Prediction]: ...


class RoboflowPredictionProvider:
    def __init__(self, api_key: str, model_id: str, model_version: str, timeout: float = 25):
        self.api_key, self.model_id, self.model_version, self.timeout = api_key, model_id, model_version, timeout

    @property
    def configured(self):
        return bool(self.api_key and self.model_id and self.model_version)

    def predict(self, image: bytes) -> Optional[Prediction]:
        if not self.configured:
            return None
        response = requests.post(
            f"https://detect.roboflow.com/{self.model_id}/{self.model_version}",
            params={"api_key": self.api_key, "confidence": 0.5, "format": "json"},
            data=__import__("base64").b64encode(image).decode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=self.timeout,
        )
        response.raise_for_status()
        predictions = response.json().get("predictions") or []
        if not predictions:
            return None
        best = max(predictions, key=lambda item: float(item.get("confidence", 0)))
        return Prediction(str(best["class"]), float(best["confidence"]), "roboflow", self.model_version,
                          datetime.now(timezone.utc).isoformat())


def configured_provider(api_key: str, model_id: str, model_version: str) -> DiseasePredictionProvider:
    return RoboflowPredictionProvider(api_key, model_id, model_version)
