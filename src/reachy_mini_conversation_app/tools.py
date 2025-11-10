from __future__ import annotations
import abc
import json
import asyncio
import inspect
import logging
from typing import Any, Dict, List, Tuple, Literal
from dataclasses import dataclass

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose


logger = logging.getLogger(__name__)

# Initialize dance and emotion libraries
try:
    from reachy_mini.motion.recorded_move import RecordedMoves
    from reachy_mini_dances_library.collection.dance import AVAILABLE_MOVES
    from reachy_mini_conversation_app.dance_emotion_moves import (
        GotoQueueMove,
        DanceQueueMove,
        EmotionQueueMove,
    )

    # Initialize recorded moves for emotions
    # Note: huggingface_hub automatically reads HF_TOKEN from environment variables
    RECORDED_MOVES = RecordedMoves("pollen-robotics/reachy-mini-emotions-library")
    DANCE_AVAILABLE = True
    EMOTION_AVAILABLE = True
except ImportError as e:
    logger.warning(f"Dance/emotion libraries not available: {e}")
    AVAILABLE_MOVES = {}
    RECORDED_MOVES = None
    DANCE_AVAILABLE = False
    EMOTION_AVAILABLE = False


def get_concrete_subclasses(base: type[Tool]) -> List[type[Tool]]:
    """Recursively find all concrete (non-abstract) subclasses of a base class."""
    result: List[type[Tool]] = []
    for cls in base.__subclasses__():
        if not inspect.isabstract(cls):
            result.append(cls)
        # recurse into subclasses
        result.extend(get_concrete_subclasses(cls))
    return result


# Types & state
Direction = Literal["left", "right", "up", "down", "front"]


@dataclass
class ToolDependencies:
    """External dependencies injected into tools."""

    reachy_mini: ReachyMini
    movement_manager: Any  # MovementManager from moves.py
    # Optional deps
    camera_worker: Any | None = None  # CameraWorker for frame buffering
    vision_manager: Any | None = None
    head_wobbler: Any | None = None  # HeadWobbler for audio-reactive motion
    motion_duration_s: float = 1.0


# Tool base class
class Tool(abc.ABC):
    """Base abstraction for tools used in function-calling.

    Each tool must define:
      - name: str
      - description: str
      - parameters_schema: Dict[str, Any]  # JSON Schema
    """

    name: str
    description: str
    parameters_schema: Dict[str, Any]

    def spec(self) -> Dict[str, Any]:
        """Return the function spec for LLM consumption."""
        return {
            "type": "function",
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters_schema,
        }

    @abc.abstractmethod
    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Async tool execution entrypoint."""
        raise NotImplementedError


# Concrete tools


class MoveHead(Tool):
    """Move head in a given direction."""

    name = "move_head"
    description = "Move your head in a given direction: left, right, up, down or front."
    parameters_schema = {
        "type": "object",
        "properties": {
            "direction": {
                "type": "string",
                "enum": ["left", "right", "up", "down", "front"],
            },
        },
        "required": ["direction"],
    }

    # mapping: direction -> args for create_head_pose
    DELTAS: Dict[str, Tuple[int, int, int, int, int, int]] = {
        "left": (0, 0, 0, 0, 0, 40),
        "right": (0, 0, 0, 0, 0, -40),
        "up": (0, 0, 0, 0, -30, 0),
        "down": (0, 0, 0, 0, 30, 0),
        "front": (0, 0, 0, 0, 0, 0),
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Move head in a given direction."""
        direction_raw = kwargs.get("direction")
        if not isinstance(direction_raw, str):
            return {"error": "direction must be a string"}
        direction: Direction = direction_raw  # type: ignore[assignment]
        logger.info("Tool call: move_head direction=%s", direction)

        deltas = self.DELTAS.get(direction, self.DELTAS["front"])
        target = create_head_pose(*deltas, degrees=True)

        # Use new movement manager
        try:
            movement_manager = deps.movement_manager

            # Get current state for interpolation
            current_head_pose = deps.reachy_mini.get_current_head_pose()
            _, current_antennas = deps.reachy_mini.get_current_joint_positions()

            # Create goto move
            goto_move = GotoQueueMove(
                target_head_pose=target,
                start_head_pose=current_head_pose,
                target_antennas=(0, 0),  # Reset antennas to default
                start_antennas=(
                    current_antennas[0],
                    current_antennas[1],
                ),  # Skip body_yaw
                target_body_yaw=0,  # Reset body yaw
                start_body_yaw=current_antennas[0],  # body_yaw is first in joint positions
                duration=deps.motion_duration_s,
            )

            movement_manager.queue_move(goto_move)
            movement_manager.set_moving_state(deps.motion_duration_s)

            return {"status": f"looking {direction}"}

        except Exception as e:
            logger.error("move_head failed")
            return {"error": f"move_head failed: {type(e).__name__}: {e}"}


class Camera(Tool):
    """Check if a person is in front of the camera using face detection."""

    name = "camera"
    description = "Check if a person is detected in front of the camera. Returns whether a person is present or not."
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Question about person detection (e.g., 'Is someone there?', 'Can you see anyone?')",
            },
        },
        "required": ["question"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Take a picture with the camera and detect if a person is present."""
        image_query = (kwargs.get("question") or "").strip()
        if not image_query:
            logger.warning("camera: empty question")
            return {"error": "question must be a non-empty string"}

        logger.info("Tool call: camera question=%s", image_query[:120])

        # Get frame from camera worker buffer
        if deps.camera_worker is not None:
            frame = deps.camera_worker.get_latest_frame()
            if frame is None:
                logger.error("No frame available from camera worker")
                return {"error": "No frame available"}
        else:
            logger.error("Camera worker not available")
            return {"error": "Camera worker not available"}

        # Use head tracker for simple person detection (primary method)
        if deps.camera_worker.head_tracker is not None:
            try:
                eye_center, _ = deps.camera_worker.head_tracker.get_head_position(frame)

                if eye_center is not None:
                    return {
                        "person_detected": True,
                        "status": "A person is detected in front of the camera"
                    }
                else:
                    return {
                        "person_detected": False,
                        "status": "No person detected in front of the camera"
                    }
            except Exception as e:
                logger.error(f"Head tracker error: {e}")
                return {"error": f"Person detection failed: {e}"}

        # Fallback: Use vision manager if available (for local processing)
        if deps.vision_manager is not None:
            vision_result = await asyncio.to_thread(
                deps.vision_manager.processor.process_image, frame, image_query,
            )
            if isinstance(vision_result, dict) and "error" in vision_result:
                return vision_result
            return (
                {"image_description": vision_result}
                if isinstance(vision_result, str)
                else {"error": "vision returned non-string"}
            )

        # No detection method available
        return {"error": "No person detection method available (head tracker or vision manager required)"}


class HeadTracking(Tool):
    """Toggle head tracking state."""

    name = "head_tracking"
    description = "Toggle head tracking state."
    parameters_schema = {
        "type": "object",
        "properties": {"start": {"type": "boolean"}},
        "required": ["start"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Enable or disable head tracking."""
        enable = bool(kwargs.get("start"))

        # Update camera worker head tracking state
        if deps.camera_worker is not None:
            deps.camera_worker.set_head_tracking_enabled(enable)

        status = "started" if enable else "stopped"
        logger.info("Tool call: head_tracking %s", status)
        return {"status": f"head tracking {status}"}



def get_available_emotions_and_descriptions() -> str:
    """Get formatted list of available emotions with descriptions."""
    if not EMOTION_AVAILABLE:
        return "Emotions not available"

    try:
        emotion_names = RECORDED_MOVES.list_moves()
        output = "Available emotions:\n"
        for name in emotion_names:
            description = RECORDED_MOVES.get(name).description
            output += f" - {name}: {description}\n"
        return output
    except Exception as e:
        return f"Error getting emotions: {e}"

class PlayEmotion(Tool):
    """Play a pre-recorded emotion."""

    name = "play_emotion"
    description = "Play a pre-recorded emotion"
    parameters_schema = {
        "type": "object",
        "properties": {
            "emotion": {
                "type": "string",
                "description": f"""Name of the emotion to play.
                                    Here is a list of the available emotions:
                                    {get_available_emotions_and_descriptions()}
                                    """,
            },
        },
        "required": ["emotion"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Play a pre-recorded emotion."""
        if not EMOTION_AVAILABLE:
            return {"error": "Emotion system not available"}

        emotion_name = kwargs.get("emotion")
        if not emotion_name:
            return {"error": "Emotion name is required"}

        logger.info("Tool call: play_emotion emotion=%s", emotion_name)

        # Check if emotion exists
        try:
            emotion_names = RECORDED_MOVES.list_moves()
            if emotion_name not in emotion_names:
                return {"error": f"Unknown emotion '{emotion_name}'. Available: {emotion_names}"}

            # Add emotion to queue
            movement_manager = deps.movement_manager
            emotion_move = EmotionQueueMove(emotion_name, RECORDED_MOVES)
            movement_manager.queue_move(emotion_move)

            return {"status": "queued", "emotion": emotion_name}

        except Exception as e:
            logger.exception("Failed to play emotion")
            return {"error": f"Failed to play emotion: {e!s}"}


class StopEmotion(Tool):
    """Stop the current emotion."""

    name = "stop_emotion"
    description = "Stop the current emotion"
    parameters_schema = {
        "type": "object",
        "properties": {
            "dummy": {
                "type": "boolean",
                "description": "dummy boolean, set it to true",
            },
        },
        "required": ["dummy"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Stop the current emotion."""
        logger.info("Tool call: stop_emotion")
        movement_manager = deps.movement_manager
        movement_manager.clear_move_queue()
        return {"status": "stopped emotion and cleared queue"}


class LogPersonData(Tool):
    """Log information about a person you've met for future connection matching."""

    name = "log_person_data"
    description = """Log information about a person you've met, including their name, background,
    hobbies, interests, email, and visual description. Use this at the END of a successful conversation
    to save the person's data for future matching."""
    parameters_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "The person's name",
            },
            "background": {
                "type": "string",
                "description": "What they do - work, study, projects, etc.",
            },
            "hobbies": {
                "type": "string",
                "description": "Their hobbies and interests",
            },
            "interests": {
                "type": "string",
                "description": "What they're passionate about or want to explore",
            },
            "email": {
                "type": "string",
                "description": "Their email address for sending introductions",
            },
            "visual_description": {
                "type": "string",
                "description": "Visual details about what they were wearing (from camera)",
            },
            "notes": {
                "type": "string",
                "description": "Any additional notes or memorable details from the conversation",
            },
        },
        "required": ["name", "background", "hobbies", "email"],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Log person data to file and structured logs."""
        import datetime
        import os

        # Extract parameters
        name = kwargs.get("name", "Unknown")
        background = kwargs.get("background", "")
        hobbies = kwargs.get("hobbies", "")
        interests = kwargs.get("interests", "")
        email = kwargs.get("email", "")
        visual_description = kwargs.get("visual_description", "")
        notes = kwargs.get("notes", "")

        # Create timestamp
        timestamp = datetime.datetime.now().isoformat()

        # Create data structure
        person_data = {
            "timestamp": timestamp,
            "name": name,
            "background": background,
            "hobbies": hobbies,
            "interests": interests,
            "email": email,
            "visual_description": visual_description,
            "notes": notes,
        }

        # Log to structured logger
        logger.info(
            "Person data logged: name=%s email=%s background=%s hobbies=%s",
            name,
            email,
            background[:50],
            hobbies[:50],
        )

        # Append to JSON lines file for persistence
        log_file_path = os.path.expanduser("~/reachy_people_connector_data.jsonl")
        try:
            with open(log_file_path, "a") as f:
                json.dump(person_data, f)
                f.write("\n")
            logger.info("Person data written to %s", log_file_path)
        except Exception as e:
            logger.error("Failed to write to log file: %s", e)
            return {
                "status": "error",
                "error": f"Failed to save data: {e}",
            }

        return {
            "status": "success",
            "message": f"Successfully logged data for {name}. Total entries in database.",
            "saved_to": log_file_path,
        }


class DoNothing(Tool):
    """Choose to do nothing - stay still and silent. Use when you want to be contemplative or just chill."""

    name = "do_nothing"
    description = "Choose to do nothing - stay still and silent. Use when you want to be contemplative or just chill."
    parameters_schema = {
        "type": "object",
        "properties": {
            "reason": {
                "type": "string",
                "description": "Optional reason for doing nothing (e.g., 'contemplating existence', 'saving energy', 'being mysterious')",
            },
        },
        "required": [],
    }

    async def __call__(self, deps: ToolDependencies, **kwargs: Any) -> Dict[str, Any]:
        """Do nothing - stay still and silent."""
        reason = kwargs.get("reason", "just chilling")
        logger.info("Tool call: do_nothing reason=%s", reason)
        return {"status": "doing nothing", "reason": reason}


# Registry & specs (dynamic)

# List of available tool classes
ALL_TOOLS: Dict[str, Tool] = {cls.name: cls() for cls in get_concrete_subclasses(Tool)}  # type: ignore[type-abstract]
ALL_TOOL_SPECS = [tool.spec() for tool in ALL_TOOLS.values()]


# Dispatcher
def _safe_load_obj(args_json: str) -> Dict[str, Any]:
    try:
        parsed_args = json.loads(args_json or "{}")
        return parsed_args if isinstance(parsed_args, dict) else {}
    except Exception:
        logger.warning("bad args_json=%r", args_json)
        return {}


async def dispatch_tool_call(tool_name: str, args_json: str, deps: ToolDependencies) -> Dict[str, Any]:
    """Dispatch a tool call by name with JSON args and dependencies."""
    tool = ALL_TOOLS.get(tool_name)

    if not tool:
        return {"error": f"unknown tool: {tool_name}"}

    args = _safe_load_obj(args_json)
    try:
        return await tool(deps, **args)
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        logger.exception("Tool error in %s: %s", tool_name, msg)
        return {"error": msg}
