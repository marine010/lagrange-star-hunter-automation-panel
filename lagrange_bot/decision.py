from __future__ import annotations

from .config import BotConfig
from .models import Action, ActionType, BattlefieldTarget, GameState, Phase, Point, SkillState, VisibleCard


def _played_count(state: GameState, card_id: str) -> int:
    return int(state.played_counts.get(card_id, 0))


def _conditions_met(conditions: dict, state: GameState) -> tuple[bool, str]:
    after_seconds = conditions.get("after_seconds")
    if after_seconds is not None and state.now_seconds < float(after_seconds):
        return False, f"before after_seconds={after_seconds}"

    before_seconds = conditions.get("before_seconds")
    if before_seconds is not None and state.now_seconds > float(before_seconds):
        return False, f"after before_seconds={before_seconds}"

    phases = conditions.get("phases")
    if phases and state.phase.value not in phases:
        return False, f"phase {state.phase.value} not in {phases}"

    requires_any = conditions.get("requires_played_any")
    if requires_any:
        if not any(_played_count(state, card_id) > 0 for card_id in requires_any):
            return False, f"needs one of {requires_any}"

    requires_all = conditions.get("requires_played_all")
    if requires_all:
        missing = [card_id for card_id in requires_all if _played_count(state, card_id) <= 0]
        if missing:
            return False, f"missing played cards {missing}"

    min_cost = conditions.get("min_cost")
    if min_cost is not None and state.cost < int(min_cost):
        return False, f"cost below min_cost={min_cost}"

    max_cost = conditions.get("max_cost")
    if max_cost is not None and state.cost > int(max_cost):
        return False, f"cost above max_cost={max_cost}"

    return True, "ok"


class DeckPolicy:
    def __init__(self, config: BotConfig):
        self.config = config

    def decide(self, state: GameState) -> Action:
        if state.phase == Phase.BATTLE:
            skill_action = self._choose_skill(state)
            if skill_action:
                return skill_action
            return Action.wait("battle phase: no ready skill")

        start_after = self.config.policy.get("deployment", {}).get("start_after_seconds")
        if start_after is not None and state.now_seconds < float(start_after):
            return Action.wait(f"waiting until deployment start_after_seconds={start_after}")

        if state.phase == Phase.PLACEMENT:
            wait_action = self._wait_for_all_hand_slots_playable(state)
            if wait_action:
                return wait_action

        if state.cost < int(self.config.policy.get("min_cost_to_act", 0)):
            return Action.wait("cost below policy min_cost_to_act")

        sequence_action = self._choose_opening_sequence(state)
        if sequence_action:
            return sequence_action

        deployment_action = self._choose_deployment_order(state)
        if deployment_action:
            return deployment_action

        card_action = self._choose_card(state)
        if card_action:
            return card_action

        skill_action = self._choose_skill(state)
        if skill_action:
            return skill_action

        return Action.wait("no valid action")

    def _choose_opening_sequence(self, state: GameState) -> Action | None:
        sequence = list(self.config.policy.get("opening_sequence", []))
        for card_id in sequence:
            if _played_count(state, card_id) <= 0:
                visible = self._find_visible_card(state, card_id)
                if visible is None:
                    return None
                action = self._card_to_action(visible, state)
                if action.type == ActionType.PLAY_CARD:
                    return action
                return None
        return None

    def _choose_deployment_order(self, state: GameState) -> Action | None:
        deployment = self.config.policy.get("deployment", {})
        priority_order = list(deployment.get("priority_order", []))
        fallback_order = list(deployment.get("fallback_order", []))
        if not priority_order and not fallback_order:
            return None

        wait_for_unaffordable_priority = bool(deployment.get("wait_for_unaffordable_priority", True))
        if state.phase == Phase.EXTRA_PLACE:
            wait_for_unaffordable_priority = bool(
                deployment.get("extra_place_wait_for_unaffordable_priority", False)
            )

        priority_visible = [card_id for card_id in priority_order if self._find_visible_card(state, card_id)]
        for card_id in priority_order:
            visible = self._find_visible_card(state, card_id)
            if not visible:
                continue
            if state.cost >= visible.card.cost:
                action = self._card_to_action(visible, state)
                if action.type == ActionType.PLAY_CARD:
                    return action
            if wait_for_unaffordable_priority:
                return Action.wait(f"visible priority card {visible.card.name}, waiting for cost {visible.card.cost}")

        fallback_when_no_priority_visible = bool(deployment.get("fallback_when_no_priority_visible", True))
        if (priority_visible and wait_for_unaffordable_priority) or not fallback_when_no_priority_visible:
            return None

        for card_id in fallback_order:
            visible = self._find_visible_card(state, card_id)
            if not visible or state.cost < visible.card.cost:
                continue
            action = self._card_to_action(visible, state)
            if action.type == ActionType.PLAY_CARD:
                return action

        return None

    def _wait_for_all_hand_slots_playable(self, state: GameState) -> Action | None:
        deployment = self.config.policy.get("deployment", {})
        if not bool(deployment.get("wait_for_all_hand_slots_playable", False)):
            return None
        if len(state.hand_slot_playable) < len(self.config.hand_slots):
            return None
        dark_slots = [
            slot.name
            for slot in self.config.hand_slots
            if not state.hand_slot_playable.get(slot.name, False)
        ]
        if not dark_slots:
            return None
        return Action.wait(f"deployment waiting for all hand cards playable; dark_slots={dark_slots}")

    def _choose_card(self, state: GameState) -> Action | None:
        candidates: list[VisibleCard] = []
        for visible in state.visible_cards:
            card = visible.card
            if state.cost < card.cost:
                continue
            if _played_count(state, card.id) >= card.max_plays:
                continue
            ok, _reason = _conditions_met(card.conditions, state)
            if not ok:
                continue
            candidates.append(visible)

        if not candidates:
            return None

        prefer_cost = bool(self.config.policy.get("prefer_higher_cost_when_priority_ties", True))
        candidates.sort(
            key=lambda item: (
                item.card.priority,
                item.card.cost if prefer_cost else -item.card.cost,
                item.confidence,
            ),
            reverse=True,
        )
        return self._card_to_action(candidates[0], state)

    def _choose_skill(self, state: GameState) -> Action | None:
        candidates: list[SkillState] = []
        for skill_state in state.skills:
            skill = skill_state.skill
            if not skill.active or not skill_state.ready or skill.click is None:
                continue
            ok, _reason = _conditions_met(skill.conditions, state)
            if ok:
                candidates.append(skill_state)

        if not candidates:
            return None

        candidates.sort(key=lambda item: (item.skill.priority, item.confidence), reverse=True)
        chosen = candidates[0]
        select_click = self._select_skill_target(chosen.skill.select_target_group, state)
        if select_click is None:
            select_click = chosen.skill.select_click
        return Action(
            type=ActionType.CAST_SKILL,
            skill_id=chosen.skill.id,
            click=chosen.skill.click,
            target_click=select_click or chosen.skill.target_click,
            reason=f"cast skill {chosen.skill.name}",
        )

    def _select_skill_target(self, target_group: str | None, state: GameState) -> Point | None:
        if not target_group:
            return None
        battle = self.config.policy.get("battle", {})
        selector = battle.get("dynamic_target_groups", {}).get(target_group)
        if not selector:
            return None

        target_id = str(selector.get("target_id", "cas066_battle_label"))
        candidates = [target for target in state.battlefield_targets if target.target_id == target_id]
        min_candidates = max(1, int(selector.get("min_candidates", 1)))
        if len(candidates) < min_candidates:
            return None

        candidates = self._sort_battlefield_targets(candidates, str(selector.get("sort", "topmost")))
        index = int(selector.get("index", 0))
        if index < 0:
            index = len(candidates) + index
        index = max(0, min(index, len(candidates) - 1))
        return candidates[index].click

    @staticmethod
    def _sort_battlefield_targets(
        targets: list[BattlefieldTarget],
        mode: str,
    ) -> list[BattlefieldTarget]:
        if mode == "bottommost":
            return sorted(targets, key=lambda item: (item.click[1], item.click[0]), reverse=True)
        if mode == "leftmost":
            return sorted(targets, key=lambda item: (item.click[0], item.click[1]))
        if mode == "rightmost":
            return sorted(targets, key=lambda item: (item.click[0], item.click[1]), reverse=True)
        if mode == "highest_confidence":
            return sorted(targets, key=lambda item: item.confidence, reverse=True)
        return sorted(targets, key=lambda item: (item.click[1], item.click[0]))

    @staticmethod
    def _find_visible_card(state: GameState, card_id: str) -> VisibleCard | None:
        for visible in state.visible_cards:
            if visible.card.id == card_id:
                return visible
        return None

    @staticmethod
    def _card_to_action(visible: VisibleCard, state: GameState) -> Action:
        card = visible.card
        if state.cost < card.cost:
            return Action.wait(f"visible {card.name}, waiting for cost {card.cost}")
        if _played_count(state, card.id) >= card.max_plays:
            return Action.wait(f"{card.name} reached max_plays")
        ok, reason = _conditions_met(card.conditions, state)
        if not ok:
            return Action.wait(f"{card.name} condition blocked: {reason}")
        return Action(
            type=ActionType.PLAY_CARD,
            card_id=card.id,
            click=visible.slot.click,
            reason=f"play {card.name} from {visible.slot.name}",
        )
