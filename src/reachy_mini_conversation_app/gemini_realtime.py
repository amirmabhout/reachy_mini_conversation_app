import json
import base64
import asyncio
from asyncio import Queue
import logging
from typing import Any, Tuple, Literal, cast
from datetime import datetime

import numpy as np
import gradio as gr
from google import genai
from google.genai import types
from fastrtc import AdditionalOutputs, AsyncStreamHandler, wait_for_item
from numpy.typing import NDArray

from reachy_mini_conversation_app.tools import (
    ALL_TOOLS,
    ToolDependencies,
    dispatch_tool_call,
)
from reachy_mini_conversation_app.config import config
from reachy_mini_conversation_app.prompts import SESSION_INSTRUCTIONS


logger = logging.getLogger(__name__)


def convert_tools_to_gemini_format() -> list[types.FunctionDeclaration]:
    """Convert OpenAI tool specs to Gemini function declarations."""
    gemini_functions = []

    for tool in ALL_TOOLS.values():
        # Get the OpenAI spec
        spec = tool.spec()

        # Convert to Gemini format
        function_declaration = types.FunctionDeclaration(
            name=spec["name"],
            description=spec["description"],
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties={
                    param_name: types.Schema(
                        type=_convert_json_type_to_gemini(param_spec.get("type", "string")),
                        description=param_spec.get("description", ""),
                        enum=param_spec.get("enum"),
                    )
                    for param_name, param_spec in spec["parameters"].get("properties", {}).items()
                },
                required=spec["parameters"].get("required", []),
            ),
        )
        gemini_functions.append(function_declaration)

    return gemini_functions


def _convert_json_type_to_gemini(json_type: str) -> types.Type:
    """Convert JSON schema type to Gemini Type enum."""
    type_mapping = {
        "string": types.Type.STRING,
        "number": types.Type.NUMBER,
        "integer": types.Type.INTEGER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
        "object": types.Type.OBJECT,
    }
    return type_mapping.get(json_type.lower(), types.Type.STRING)


class GeminiRealtimeHandler(AsyncStreamHandler):
    """A Gemini Live API handler for fastrtc Stream."""

    def __init__(self, deps: ToolDependencies):
        """Initialize the handler."""
        super().__init__(
            expected_layout="mono",
            output_sample_rate=24000,  # Gemini outputs 24kHz
            input_sample_rate=16000,  # respeaker output
        )
        self.deps = deps

        # Override type annotations for strict typing
        self.output_sample_rate: Literal[24000]
        self.target_input_rate: Literal[16000] = 16000  # Gemini supports 16kHz input
        self.resample_ratio = self.target_input_rate / self.input_sample_rate

        self.session: Any = None
        self.output_queue: "asyncio.Queue[Tuple[int, NDArray[np.int16]] | AdditionalOutputs]" = asyncio.Queue()
        self.send_queue: Queue = Queue()
        self._running = False

        self.last_activity_time = asyncio.get_event_loop().time()
        self.start_time = asyncio.get_event_loop().time()
        self.is_idle_tool_call = False

        # Initialize Gemini client
        self.client = genai.Client(
            http_options={"api_version": "v1beta"},
            api_key=config.GEMINI_API_KEY,
        )

    def copy(self) -> "GeminiRealtimeHandler":
        """Create a copy of the handler."""
        return GeminiRealtimeHandler(self.deps)

    def resample_audio(self, audio: NDArray[np.int16]) -> NDArray[np.int16]:
        """Resample audio using linear interpolation."""
        if self.input_sample_rate == self.target_input_rate:
            return audio

        # Use numpy's interp for simple linear resampling
        input_length = len(audio)
        output_length = int(input_length * self.resample_ratio)

        input_time = np.arange(input_length)
        output_time = np.linspace(0, input_length - 1, output_length)

        resampled = np.interp(output_time, input_time, audio.astype(np.float32))
        return cast(NDArray[np.int16], resampled.astype(np.int16))

    async def _receive_loop(self):
        """Dedicated task for receiving responses from Gemini."""
        while self._running:
            try:
                turn = self.session.receive()
                async for response in turn:
                    await self._handle_response(response)
            except Exception as e:
                if self._running:
                    logger.error("Error in receive loop: %s", e)
                break

    async def _send_loop(self):
        """Dedicated task for sending queued messages to Gemini."""
        while self._running:
            try:
                message = await self.send_queue.get()
                if message is None:  # Shutdown signal
                    break

                # Handle tool responses separately
                if message.get("type") == "tool_response":
                    await self.session.send_tool_response(
                        function_responses=message["function_responses"]
                    )
                else:
                    await self.session.send(**message)

                self.send_queue.task_done()
            except Exception as e:
                if self._running:
                    logger.warning("Failed to send to Gemini: %s", e)

    async def start_up(self) -> None:
        """Set up and manage the Gemini Live session with concurrent send/receive tasks."""
        logger.info("Starting Gemini handler...")

        try:
            # Convert tools to Gemini format
            gemini_tools = convert_tools_to_gemini_format()

            # Configure the session
            session_config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name="sadachbia"  # Using Sadachbia as a neutral voice
                        )
                    )
                ),
                system_instruction=types.Content(
                    parts=[types.Part(text=SESSION_INSTRUCTIONS)]
                ),
                tools=[types.Tool(function_declarations=gemini_tools)],
            )

            logger.info("Connecting to Gemini Live API...")

            # Connect to Gemini Live API and run send/receive tasks concurrently
            async with self.client.aio.live.connect(
                model=config.GEMINI_MODEL_NAME,
                config=session_config
            ) as session:
                self.session = session
                self._running = True
                logger.info("Gemini Live session started successfully")

                # Create concurrent tasks for send and receive
                receive_task = asyncio.create_task(self._receive_loop())
                send_task = asyncio.create_task(self._send_loop())

                # Wait for either task to complete (error or shutdown)
                done, pending = await asyncio.wait(
                    [receive_task, send_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # Clean shutdown: cancel remaining tasks
                self._running = False
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        except Exception as e:
            logger.exception("Gemini Live session failed: %s", e)
            self._running = False
            return

    async def _handle_response(self, response: Any) -> None:
        """Handle responses from Gemini Live API."""
        try:
            # DEBUG: Log the full response structure
            logger.debug("=== Gemini Response Debug ===")
            logger.debug(f"Response type: {type(response)}")
            logger.debug(f"Response attributes: {dir(response)}")
            logger.debug(f"Response repr: {repr(response)}")

            # Handle audio data
            if hasattr(response, 'data') and response.data:
                logger.debug(f"Found audio data, type: {type(response.data)}, size: {len(response.data) if hasattr(response.data, '__len__') else 'unknown'}")
                await self._handle_audio_data(response.data)

            # Handle text output - only if response contains actual text parts
            # Avoid accessing .text on audio-only responses to prevent warning flood
            if hasattr(response, 'server_content') and response.server_content:
                content = response.server_content
                if hasattr(content, 'model_turn') and content.model_turn:
                    model_turn = content.model_turn
                    if hasattr(model_turn, 'parts'):
                        # Check if any part is a text part (not just inline_data)
                        for part in model_turn.parts:
                            if hasattr(part, 'text') and part.text:
                                logger.debug(f"Found text: {part.text[:100]}...")
                                await self._handle_text_part(part.text)
                                break

            # Handle function calls
            if hasattr(response, 'function_calls') and response.function_calls:
                logger.debug(f"Found function_calls: {type(response.function_calls)}")
                logger.debug(f"function_calls repr: {repr(response.function_calls)}")

                # Check if function_calls is iterable
                try:
                    # Try to get length
                    if hasattr(response.function_calls, '__len__'):
                        logger.debug(f"function_calls length: {len(response.function_calls)}")

                    # Check if it's a list-like object or a single FunctionCall
                    if hasattr(response.function_calls, '__iter__') and not isinstance(response.function_calls, str):
                        # It's iterable, iterate through each call
                        for idx, function_call in enumerate(response.function_calls):
                            logger.debug(f"Processing function call {idx}: {type(function_call)}")
                            # Check if this is actually a FunctionCall object or still a wrapper
                            if hasattr(function_call, 'name'):
                                await self._handle_function_call(function_call)
                            else:
                                logger.error(f"Item {idx} doesn't have 'name': {type(function_call)}, {repr(function_call)}")
                    else:
                        # Might be a single call
                        logger.debug("function_calls is not iterable, treating as single call")
                        await self._handle_function_call(response.function_calls)
                except Exception as e:
                    logger.exception(f"Error iterating function_calls: {e}")

            # Handle tool responses
            if hasattr(response, 'tool_call') and response.tool_call:
                logger.debug(f"Found tool_call: {type(response.tool_call)}")
                await self._handle_function_call(response.tool_call)

            logger.debug("=== End Response Debug ===")

        except Exception as e:
            logger.exception("Error handling Gemini response: %s", e)

    async def _handle_audio_data(self, audio_data: Any) -> None:
        """Handle audio data from Gemini."""
        try:
            # DEBUG: Log audio data details
            logger.debug(f"Audio data type: {type(audio_data)}")
            logger.debug(f"Audio data length: {len(audio_data) if hasattr(audio_data, '__len__') else 'N/A'}")

            # Gemini sends audio data - determine if it's base64 string or raw bytes
            if isinstance(audio_data, str):
                # It's a base64 string, decode it
                logger.debug("Audio data is base64 string, decoding...")
                audio_bytes = base64.b64decode(audio_data)
                audio_b64 = audio_data  # For head wobbler
            elif isinstance(audio_data, bytes):
                # It's already bytes
                logger.debug("Audio data is raw bytes, using directly...")
                audio_bytes = audio_data
                # Encode to base64 for head wobbler
                audio_b64 = base64.b64encode(audio_data).decode('utf-8')
            else:
                logger.error(f"Unexpected audio data type: {type(audio_data)}")
                return

            logger.debug(f"Audio bytes length: {len(audio_bytes)}")

            # Ensure audio bytes length is multiple of 2 (for int16)
            if len(audio_bytes) % 2 != 0:
                logger.warning(f"Audio bytes length {len(audio_bytes)} is not multiple of 2, truncating...")
                audio_bytes = audio_bytes[:-(len(audio_bytes) % 2)]

            # Convert to numpy array
            audio_array = np.frombuffer(audio_bytes, dtype=np.int16)
            logger.debug(f"Audio array shape: {audio_array.shape}")

            # Feed to head wobbler
            if self.deps.head_wobbler is not None:
                # Head wobbler expects base64
                self.deps.head_wobbler.feed(audio_b64)

            # Update activity time
            self.last_activity_time = asyncio.get_event_loop().time()
            logger.debug("last activity time updated to %s", self.last_activity_time)

            # Queue for playback
            await self.output_queue.put(
                (
                    self.output_sample_rate,
                    audio_array.reshape(1, -1),
                )
            )
        except Exception as e:
            logger.exception(f"Error handling audio data (type: {type(audio_data)}): %s", e)

    async def _handle_text_part(self, text: str) -> None:
        """Handle text output (transcription) from Gemini."""
        logger.debug(f"Assistant transcript: {text}")
        await self.output_queue.put(
            AdditionalOutputs({"role": "assistant", "content": text})
        )

    async def _handle_function_call(self, function_call: Any) -> None:
        """Handle function call from Gemini."""
        try:
            # DEBUG: Log function call structure
            logger.debug(f"=== Function Call Debug ===")
            logger.debug(f"Function call type: {type(function_call)}")
            logger.debug(f"Function call attributes: {dir(function_call)}")
            logger.debug(f"Function call repr: {repr(function_call)}")

            # Check if this is a LiveServerToolCall wrapper with function_calls list inside
            if hasattr(function_call, 'function_calls'):
                logger.debug("Found function_calls attribute, extracting actual function calls...")
                function_calls_list = function_call.function_calls
                logger.debug(f"function_calls list type: {type(function_calls_list)}, length: {len(function_calls_list) if hasattr(function_calls_list, '__len__') else 'N/A'}")

                # Iterate through actual function calls
                for idx, actual_call in enumerate(function_calls_list):
                    logger.debug(f"Processing function call {idx}: {type(actual_call)}, repr: {repr(actual_call)}")
                    # Recurse with the actual FunctionCall object
                    await self._handle_function_call(actual_call)
                return  # We've handled all function calls

            # Extract tool name - this should be a FunctionCall object now
            if not hasattr(function_call, 'name'):
                logger.error(f"Function call missing 'name' attribute: {type(function_call)}, {repr(function_call)}")
                return

            tool_name = function_call.name
            logger.debug(f"Extracted tool name: {tool_name}")

            # Extract function call ID if available
            call_id = getattr(function_call, 'id', None)
            logger.debug(f"Function call ID: {call_id}")

            if not call_id:
                logger.error(f"Function call missing ID: {type(function_call)}, {repr(function_call)}")
                # Generate a fallback ID to prevent errors
                call_id = f"fallback_{tool_name}_{asyncio.get_event_loop().time()}"
                logger.warning(f"Using fallback ID: {call_id}")

            # Extract arguments
            if hasattr(function_call, 'args') and function_call.args:
                logger.debug(f"Args type: {type(function_call.args)}")
                logger.debug(f"Args content: {function_call.args}")
                # Convert args to JSON string
                args_dict = dict(function_call.args)
                args_json_str = json.dumps(args_dict)
            else:
                logger.debug("No args found, using empty dict")
                args_json_str = "{}"

            logger.info(f"Tool call: {tool_name} with args: {args_json_str}")

            # Execute the tool
            try:
                tool_result = await dispatch_tool_call(tool_name, args_json_str, self.deps)
                logger.debug("Tool '%s' executed successfully", tool_name)
                logger.debug("Tool result: %s", tool_result)
            except Exception as e:
                logger.error("Tool '%s' failed: %s", tool_name, e)
                tool_result = {"error": str(e)}

            # Send tool result back to Gemini using the correct API
            # Create FunctionResponse object with the function call ID
            function_response = types.FunctionResponse(
                id=call_id,
                name=tool_name,
                response=tool_result
            )

            await self.send_queue.put({
                "type": "tool_response",
                "function_responses": [function_response]
            })

            # Display tool usage in UI
            await self.output_queue.put(
                AdditionalOutputs(
                    {
                        "role": "assistant",
                        "content": json.dumps(tool_result),
                        "metadata": {"title": f"🛠️ Used tool {tool_name}", "status": "done"},
                    }
                )
            )

            # Handle camera tool - display image in UI (person detection result)
            if tool_name == "camera" and self.deps.camera_worker is not None:
                np_img = self.deps.camera_worker.get_latest_frame()
                if np_img is not None:
                    img = gr.Image(value=np_img)

                    await self.output_queue.put(
                        AdditionalOutputs(
                            {
                                "role": "assistant",
                                "content": img,
                            }
                        )
                    )

            # Reset head wobbler after tool call
            if self.deps.head_wobbler is not None:
                self.deps.head_wobbler.reset()

        except Exception as e:
            logger.exception("Error handling function call: %s", e)

    async def receive(self, frame: Tuple[int, NDArray[np.int16]]) -> None:
        """Receive audio frame from the microphone and send it to Gemini."""
        if not self.session:
            return

        _, array = frame
        array = array.squeeze()

        # Resample if needed (though Gemini accepts 16kHz, so usually no resampling)
        if self.input_sample_rate != self.target_input_rate:
            array = self.resample_audio(array)

        # Convert to PCM bytes
        audio_bytes = array.tobytes()

        # Queue message to be sent by the send loop
        try:
            await self.send_queue.put({
                "input": {"data": audio_bytes, "mime_type": "audio/pcm;rate=16000"}
            })
        except Exception as e:
            logger.warning("Failed to queue audio for Gemini: %s", e)

    async def emit(self) -> Tuple[int, NDArray[np.int16]] | AdditionalOutputs | None:
        """Emit audio frame to be played by the speaker."""
        # Handle idle
        idle_duration = asyncio.get_event_loop().time() - self.last_activity_time
        if idle_duration > 15.0 and self.deps.movement_manager.is_idle():
            try:
                await self.send_idle_signal(idle_duration)
            except Exception as e:
                logger.warning("Idle signal skipped (connection closed?): %s", e)
                return None

            self.last_activity_time = asyncio.get_event_loop().time()

        return await wait_for_item(self.output_queue)  # type: ignore[no-any-return]

    async def shutdown(self) -> None:
        """Shutdown the handler."""
        logger.info("Shutting down Gemini handler...")
        self._running = False

        # Signal send loop to stop
        try:
            await self.send_queue.put(None)
        except Exception as e:
            logger.warning("Error signaling send loop: %s", e)

        # Clear session
        self.session = None
        logger.info("Gemini session closed")

        # Clear any remaining items in the output queue
        while not self.output_queue.empty():
            try:
                self.output_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def format_timestamp(self) -> str:
        """Format current timestamp with date, time, and elapsed seconds."""
        loop_time = asyncio.get_event_loop().time()
        elapsed_seconds = loop_time - self.start_time
        dt = datetime.now()
        return f"[{dt.strftime('%Y-%m-%d %H:%M:%S')} | +{elapsed_seconds:.1f}s]"

    async def send_idle_signal(self, idle_duration: float) -> None:
        """Send an idle signal to Gemini."""
        logger.debug("Sending idle signal")
        self.is_idle_tool_call = True
        timestamp_msg = f"[Idle time update: {self.format_timestamp()} - No activity for {idle_duration:.1f}s] You've been idle for a while. Feel free to get creative - dance, show an emotion, look around, do nothing, or just be yourself!"

        if not self.session:
            logger.debug("No connection, cannot send idle signal")
            return

        try:
            await self.send_queue.put({
                "input": timestamp_msg,
                "end_of_turn": True
            })
        except Exception as e:
            logger.warning("Failed to queue idle signal: %s", e)
