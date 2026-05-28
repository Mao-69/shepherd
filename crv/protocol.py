"""crv/protocol.py — CRV stage definitions and session structure.

Based on Ingo Swann's six-stage Controlled Remote Viewing protocol as
documented in the partially-declassified DIA "Coordinate Remote Viewing
Manual" (1985) and subsequent open literature, plus Monroe Institute
Gateway Experience timing guidance.

Three timing tiers are provided:

    PROTOCOL_SHORT     ~ 60 min — compressed practice / time-limited
    PROTOCOL_STANDARD  ~ 95 min — closer to Monroe / SRI literature  ← default
    PROTOCOL_LONG      ~150 min — full SRI-spec deep work

Two protocol shapes:

    STAGES_STANDARD   — baseline → induction → CRV I-VI → cooldown
    STAGES_EXTENDED   — same plus Focus 21-27 journey before cooldown

Combined: timing × shape gives 6 protocols total. Choose with:
    use_short() / use_standard() / use_long()    (timing tier)
    use_extended()                                (adds Focus 21-27)

Literature notes on the timing (these durations come from open publications,
not the proprietary Monroe Institute curricula):

  * Focus 10 induction: Monroe materials recommend 20-30 min minimum for
    new practitioners to drop in reliably ("Gateway Experience" home study
    Wave I). Compressed protocols use as little as 5 min but skilled
    practitioners report that's just settling-into-chair time.
  * CRV Stage I (ideogram): SRI procedure was very fast — coordinate
    received, reflexive ideogram drawn, decoded in 15-90 seconds. The
    stage is intentionally short; longer time invites analysis.
  * CRV Stages V & VI: McMoneagle and other tested SRI viewers report
    these often running 20-30 min each, especially Stage VI for dense
    targets.

These are open-literature numbers, not Monroe Institute proprietary
program timings. Adjust to fit your practice as you accumulate data.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import List, Optional


@dataclass(frozen=True)
class Stage:
    code:           str
    name:           str
    duration_sec:   int
    focus_level:    str
    description:    str
    prompts:        List[str] = field(default_factory=list)
    cue_message:    Optional[str] = None


# -----------------------------------------------------------------------------
# Per-tier durations, in seconds. Keys correspond to stage codes.
# -----------------------------------------------------------------------------

_DURATIONS = {
    "short": {
        # The original ~60-min protocol I shipped
        "baseline":  180,    # 3 min
        "induction": 300,    # 5 min  ← acknowledged-short induction
        "stage1":    120,    # 2 min
        "stage2":    300,    # 5 min
        "stage3":    600,    # 10 min
        "stage4":    600,    # 10 min
        "stage5":    600,    # 10 min
        "stage6":    600,    # 10 min
        "cooldown":  180,    # 3 min
        "journey_21":            420,   # 7 min
        "journey_22":            300,   # 5 min
        "journey_23_to_26":      600,   # 10 min
        "journey_27":            420,   # 7 min
    },
    "standard": {
        # Closer to literature — induction extended substantially, stages
        # V and VI lengthened, cooldown lengthened. Total ~95 min.
        "baseline":  300,    # 5 min  (longer baseline = more stable HRV reference)
        "induction": 1200,   # 20 min  ← genuine Focus 10 induction time
        "stage1":     90,    # 90 sec  (ideogram is supposed to be fast)
        "stage2":    420,    # 7 min
        "stage3":    900,    # 15 min
        "stage4":    720,    # 12 min
        "stage5":   1080,    # 18 min
        "stage6":   1200,    # 20 min
        "cooldown":  300,    # 5 min
        "journey_21":            540,   # 9 min
        "journey_22":            420,   # 7 min
        "journey_23_to_26":      900,   # 15 min
        "journey_27":            600,   # 10 min
    },
    "long": {
        # Full SRI/Monroe-spec deep work. Total ~150 min for CRV portion.
        "baseline":  300,    # 5 min
        "induction": 1800,   # 30 min  ← full Monroe-recommended F10 induction
        "stage1":    120,    # 2 min
        "stage2":    600,    # 10 min
        "stage3":   1200,    # 20 min
        "stage4":    900,    # 15 min
        "stage5":   1800,    # 30 min
        "stage6":   1800,    # 30 min
        "cooldown":  600,    # 10 min
        "journey_21":            900,   # 15 min
        "journey_22":            600,   # 10 min
        "journey_23_to_26":     1500,   # 25 min
        "journey_27":            900,   # 15 min
    },
}


# -----------------------------------------------------------------------------
# Base stage definitions (durations get patched in by tier)
# -----------------------------------------------------------------------------

_BASE_STAGES: List[Stage] = [
    Stage(
        code="baseline",
        name="Pre-Session Baseline",
        duration_sec=0,                     # filled in by tier
        focus_level="off",
        description=(
            "Sit comfortably with the watch snug on your wrist. Eyes open "
            "or closed, breathe naturally. The program is recording your "
            "resting heart-rate baseline. Don't speak or move much."
        ),
        cue_message="Establishing physiological baseline...",
    ),

    Stage(
        code="induction",
        name="Induction — Focus 10",
        duration_sec=0,
        focus_level="focus_10",
        description=(
            "Put your headphones on. Eyes closed. Allow your body to relax "
            "completely while keeping your mind awake. This is Focus 10: "
            "mind awake, body asleep. Breathe slowly. Don't rush this — "
            "give yourself time to actually drop into the state. Notice "
            "the relaxation moving through your body from feet to head."
        ),
        cue_message="Beginning Focus 10 induction. Headphones on, eyes closed.",
    ),

    Stage(
        code="stage1",
        name="Stage I — Ideogram",
        duration_sec=0,
        focus_level="focus_10",
        description=(
            "The target coordinate has been presented. The instant you "
            "received it, make a quick reflexive pencil mark — the "
            "ideogram. Don't think; just let your hand respond. Then "
            "decode what your hand felt: primary motion, primary "
            "substance, primary descriptor. This stage is fast — stay "
            "reflexive, don't analyze."
        ),
        prompts=[
            "Target identifier:",
            "Ideogram (paste/describe the mark you drew):",
            "Motion / feel of the ideogram:",
            "Primary descriptor (one word):",
        ],
        cue_message="Stage I: Ideogram. Mark, then decode.",
    ),

    Stage(
        code="stage2",
        name="Stage II — Sensory Data",
        duration_sec=0,
        focus_level="focus_12",
        description=(
            "Probe each sense individually. Don't analyze — just record "
            "raw impressions as they arise. If your mind tries to label "
            "the target — an A-O-L, analytical overlay — write it down "
            "and let it pass. Probe vision, then sound, then smell, then "
            "touch, then taste, then ambient feel."
        ),
        prompts=[
            "Colors / visual impressions:",
            "Textures / surfaces:",
            "Temperatures / ambient feel:",
            "Smells:",
            "Sounds:",
            "Tastes:",
            "AOLs noticed:",
        ],
        cue_message="Stage II: Sensory data. Probe each sense in turn.",
    ),

    Stage(
        code="stage3",
        name="Stage III — Dimensions",
        duration_sec=0,
        focus_level="focus_12",
        description=(
            "Sketch the dimensional aspects of the target. Size relative "
            "to your body? Shape? Mass? Distance? Height? Is anything "
            "moving? Capture quick spatial sketches in notes.md. Take "
            "your time — let the spatial impressions deepen."
        ),
        prompts=[
            "Dimensional sketch / description of spatial structure:",
            "Size and shape:",
            "Movement / dynamics:",
            "Distance or scale impressions:",
        ],
        cue_message="Stage III: Dimensional aspects. Sketch the space.",
    ),

    Stage(
        code="stage4",
        name="Stage IV — Aesthetic Impact / AOL",
        duration_sec=0,
        focus_level="focus_12",
        description=(
            "What's the emotional flavor of the target? Awe? Calm? "
            "Industrial? Sacred? Mundane? Record each Analytic Overlay — "
            "your mind's labeling guess — as it appears, then move past "
            "it. Don't fight A-O-Ls; just name them and continue."
        ),
        prompts=[
            "Aesthetic impact / emotional content:",
            "AOLs (analytical labels your mind tried):",
            "Energetic / dynamic descriptors:",
        ],
        cue_message="Stage IV: Aesthetic impact. Catch the AOLs.",
    ),

    Stage(
        code="stage5",
        name="Stage V — Probing",
        duration_sec=0,
        focus_level="focus_15",
        description=(
            "Probe specific aspects of the target. Concrete details, "
            "function, materials, who uses it. Ask the target directly: "
            "What is this? What is its purpose? Allow concrete answers. "
            "This is a long stage. Take your time with it — the deepest "
            "data often comes after the first ten minutes."
        ),
        prompts=[
            "Concrete details / specific features:",
            "Function or purpose:",
            "Notable identifying characteristics:",
            "Materials / textures up close:",
        ],
        cue_message="Stage V: Probing for concrete details. Take your time.",
    ),

    Stage(
        code="stage6",
        name="Stage VI — Modeling",
        duration_sec=0,
        focus_level="focus_15",
        description=(
            "Synthesize everything into a model. If you had clay, what "
            "would you sculpt? Build a three-dimensional mental model. "
            "Describe spatial relationships between elements. This is "
            "the final integration — the place where the most coherent "
            "picture emerges. Don't rush; let it form."
        ),
        prompts=[
            "3D synthesis / model description:",
            "Spatial relationships:",
            "Overall best-guess summary of the target:",
        ],
        cue_message="Stage VI: Build the model. Let it form.",
    ),

    Stage(
        code="cooldown",
        name="Cool-down",
        duration_sec=0,
        focus_level="focus_10",
        description=(
            "Return slowly to ordinary consciousness. Take a few deep "
            "breaths. Feel your body in the chair, the watch on your "
            "wrist. When you're ready, open your eyes. Add any final "
            "notes or observations to notes.md."
        ),
        prompts=[
            "Final observations or anything you want to add:",
            "Confidence (1-10) in primary impressions:",
        ],
        cue_message="Cool-down. Return to ordinary consciousness.",
    ),
]


# -----------------------------------------------------------------------------
# Journey (Focus 21-27) extension stages — added via use_extended()
# -----------------------------------------------------------------------------

_BASE_JOURNEY: List[Stage] = [
    Stage(
        code="journey_21",
        name="Focus 21 — Bridge to other-energy systems",
        duration_sec=0,
        focus_level="focus_21",
        description=(
            "Allow the awareness to bridge beyond the time-space "
            "construct. Notice any sense of expansion, of moving between "
            "states. Do not steer — observe what arises."
        ),
        prompts=[
            "Sense of bridging / transition:",
            "Energetic impressions:",
            "Any visual or auditory phenomena:",
        ],
        cue_message="Entering Focus 21 — the bridge.",
    ),
    Stage(
        code="journey_22",
        name="Focus 22 — Liminal states",
        duration_sec=0,
        focus_level="focus_22",
        description=(
            "Liminal awareness. Some practitioners report encountering "
            "entities or impressions of partial consciousness here. "
            "Record without analysis."
        ),
        prompts=[
            "Any encounters or contacts:",
            "Quality of awareness here:",
        ],
        cue_message="Focus 22 — liminal.",
    ),
    Stage(
        code="journey_23_to_26",
        name="Focus 23-26 — Belief System Territories",
        duration_sec=0,
        focus_level="focus_25",
        description=(
            "Per Monroe's literature, these levels are sometimes "
            "described as zones organized around shared beliefs. Move "
            "through them with curiosity. The audio will keep you at "
            "Focus 25 as a central anchor."
        ),
        prompts=[
            "Impressions of any structured environments:",
            "Beliefs or themes that feel present here:",
        ],
        cue_message="Moving through Focus 23 to 26.",
    ),
    Stage(
        code="journey_27",
        name="Focus 27 — The park",
        duration_sec=0,
        focus_level="focus_27",
        description=(
            "Monroe described Focus 27 as a reception center — a "
            "constructed gathering place. Rest here. Notice what is "
            "spontaneous versus what is constructed by expectation."
        ),
        prompts=[
            "Description of the environment:",
            "Anyone or anything encountered:",
            "Final observations from the journey:",
        ],
        cue_message="Focus 27 — the park.",
    ),
]


# -----------------------------------------------------------------------------
# Construct the actual stage lists by patching durations into the bases.
# -----------------------------------------------------------------------------

def _apply_durations(stages: List[Stage], tier: str) -> List[Stage]:
    durs = _DURATIONS[tier]
    return [replace(s, duration_sec=durs.get(s.code, s.duration_sec))
            for s in stages]


def _build_extended(base_stages: List[Stage], journey: List[Stage]) -> List[Stage]:
    """Insert journey stages before cool-down."""
    out: List[Stage] = []
    for s in base_stages:
        if s.code == "cooldown":
            out.extend(journey)
        out.append(s)
    return out


# All six combinations:
PROTOCOLS = {}
for tier in ("short", "standard", "long"):
    base    = _apply_durations(_BASE_STAGES,  tier)
    journey = _apply_durations(_BASE_JOURNEY, tier)
    PROTOCOLS[(tier, False)] = base
    PROTOCOLS[(tier, True)]  = _build_extended(base, journey)


# Backward compatibility: the legacy names map to the current default
STAGES_STANDARD: List[Stage] = PROTOCOLS[("short", False)]
STAGES_EXTENDED: List[Stage] = PROTOCOLS[("short", True)]

# Module-level mutable that the orchestrator reads:
STAGES: List[Stage] = PROTOCOLS[("standard", False)]   # new default tier


# -----------------------------------------------------------------------------
# Switching functions used by run.py
# -----------------------------------------------------------------------------

_current_tier = "standard"
_current_extended = False

def _refresh():
    global STAGES
    STAGES = PROTOCOLS[(_current_tier, _current_extended)]


def use_short():
    global _current_tier;   _current_tier = "short";   _refresh()

def use_standard():
    global _current_tier;   _current_tier = "standard"; _refresh()

def use_long():
    global _current_tier;   _current_tier = "long";    _refresh()

def use_extended(enabled: bool = True):
    global _current_extended;  _current_extended = enabled;  _refresh()


def total_duration_sec() -> int:
    return sum(s.duration_sec for s in STAGES)


def by_code(code: str) -> Optional[Stage]:
    return next((s for s in STAGES if s.code == code), None)


def current_tier() -> str:
    return _current_tier


def current_total_minutes() -> int:
    return total_duration_sec() // 60

