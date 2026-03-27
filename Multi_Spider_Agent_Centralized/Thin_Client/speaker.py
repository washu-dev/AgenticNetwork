"""
Speaker module for PiCrawler thin client.

Provides TTS announcements and sound effects using robot_hat.
Non-blocking by default so it doesn't slow down execution.
"""

import logging
import os

from Multi_Spider_Agent_Thin_Client.config import SPEAKER_ENABLED

logger = logging.getLogger(__name__)

SOUNDS_DIR = "/home/pi/picrawler/examples/sounds"

# Map event types to sound files
SOUND_MAP = {
    "mission_start": "happy.wav",
    "detection": "bell.wav",
    "mission_complete": "happy2.wav",
    "error": "error.wav",
    "obstacle": "warning.wav",
    "alert": "vigilance.wav",
}

if SPEAKER_ENABLED:
    from robot_hat import Music, TTS
    _music = Music()
    _tts = TTS()
    _music.music_set_volume(50)
    _tts.lang("en-US")
else:
    _music = None
    _tts = None


def say(text: str):
    """Speak text aloud using local TTS."""
    if not SPEAKER_ENABLED:
        return
    try:
        _tts.say(text)
    except Exception as e:
        logger.warning(f"TTS failed: {e}")


def play_sound(event: str):
    """Play a sound effect for an event (non-blocking)."""
    if not SPEAKER_ENABLED:
        return
    filename = SOUND_MAP.get(event)
    if not filename:
        logger.warning(f"Unknown sound event: {event}")
        return

    path = os.path.join(SOUNDS_DIR, filename)
    try:
        _music.sound_play_threading(path)
    except Exception as e:
        logger.warning(f"Sound play failed: {e}")


def announce_mission_start(pattern: str = ""):
    """Announce mission start."""
    play_sound("mission_start")
    say(f"Mission started. Pattern: {pattern}" if pattern else "Mission started.")


def announce_mission_complete(detections: int = 0):
    """Announce mission complete."""
    play_sound("mission_complete")
    if detections > 0:
        say(f"Mission complete. {detections} targets found.")
    else:
        say("Mission complete. No targets found.")


def announce_error(message: str = "Error occurred"):
    """Announce an error."""
    play_sound("error")
    say(message)


def announce_obstacle():
    """Announce obstacle detected."""
    play_sound("obstacle")
