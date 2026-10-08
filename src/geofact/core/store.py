"""Implements: FA9 (LayerStore für bedarfsgesteuertes Laden), FA66 (Freigabe von Zwischenergebnissen).

LayerStore hält geladene Layer id -> LayerData. Der Executor befüllt ihn:
Layer werden erst unmittelbar vor dem ersten benötigenden Schritt geladen
(siehe Scenario.load_schedule()). Mit ``pop`` gibt er Knoten nach ihrem
letzten Leser frei (``ExecutionPlan.releases``); ``released`` merkt sie, damit
ein späterer Zugriff klar als Freigabe erkennbar ist."""

from __future__ import annotations

from geofact.core.types import LayerData


class LayerStore:
    """id -> geladener Layer. Wird vom Executor (FA9) befüllt: Layer werden
    erst unmittelbar vor dem ersten benötigenden Schritt geladen."""

    def __init__(self) -> None:
        self._layers: dict[str, LayerData] = {}
        self.released: set[str] = set()

    def __contains__(self, layer_id: str) -> bool:
        return layer_id in self._layers

    def __getitem__(self, layer_id: str) -> LayerData:
        try:
            return self._layers[layer_id]
        except KeyError:
            if layer_id in self.released:
                raise KeyError(
                    f"Layer '{layer_id}' ist nicht geladen (nach dem letzten Leser freigegeben)"
                ) from None
            raise KeyError(f"Layer '{layer_id}' ist nicht geladen") from None

    def __setitem__(self, layer_id: str, data: LayerData) -> None:
        self._layers[layer_id] = data
        self.released.discard(layer_id)

    def __len__(self) -> int:
        return len(self._layers)

    def get(self, layer_id: str) -> LayerData | None:
        return self._layers.get(layer_id)

    def pop(self, layer_id: str) -> LayerData:
        """Entfernt einen Knoten und liefert ihn (FA66); unbekannt ist ein
        ``KeyError("Layer 'x' ist nicht geladen")``."""
        try:
            data = self._layers.pop(layer_id)
        except KeyError:
            raise KeyError(f"Layer '{layer_id}' ist nicht geladen") from None
        self.released.add(layer_id)
        return data
