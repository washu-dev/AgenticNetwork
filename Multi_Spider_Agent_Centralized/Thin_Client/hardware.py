"""
Hardware driver: PiCrawler servo control with ultrasonic obstacle avoidance.

Talks directly to PiCrawler servos and ultrasonic sensor.
"""

import threading
import time
import signal
import logging

from agent_spider.config import OBSTACLE_THRESHOLD_CM, PATROL_SPEED

logger = logging.getLogger(__name__)

from picrawler import Picrawler
from robot_hat import Ultrasonic, Pin


class Timeout(Exception):
    pass


class Cancelled(Exception):
    pass


def _alarm_handler(signum, frame):
    raise Timeout()


class CrawlerControl:
    """
    Controls PiCrawler movement with ultrasonic obstacle avoidance.
    """

    def __init__(self, speed: int = PATROL_SPEED):
        self.speed = speed
        self._standing = False
        self._cancel = threading.Event()
        self.crawler = Picrawler()
        self.sonar = Ultrasonic(Pin("D2"), Pin("D3"))
        signal.signal(signal.SIGALRM, _alarm_handler)
        logger.info("CrawlerControl initialized with hardware")

    def cancel(self):
        """Signal the current action to stop."""
        self._cancel.set()

    def stop(self):
        """Emergency stop: cancel current action, freeze servos, then return to stand."""
        self._cancel.set()
        try:
            # Freeze servos in place immediately
            positions = list(self.crawler.servo_positions)
            self.crawler.servo_write_all(positions)
            logger.info("Emergency stop: servos frozen, returning to stand")
            # Return to stand posture
            self.crawler.do_action("stand", 1, self.speed)
            self._standing = True
        except Exception as e:
            logger.error(f"Emergency stop failed: {e}")

    def _wait(self, seconds: float):
        """Interruptible sleep. Raises Cancelled if cancel was requested."""
        if self._cancel.wait(seconds):
            raise Cancelled()

    def _check(self):
        """Check cancel flag between steps."""
        if self._cancel.is_set():
            raise Cancelled()

    def stand(self):
        """Stand up."""
        self.crawler.do_action("stand", 1, self.speed)
        self._wait(0.5)
        self._standing = True

    def sit(self):
        """Sit down."""
        self.crawler.do_step("sit", self.speed)
        self._wait(0.5)
        self._standing = False

    def forward(self, steps: int = 1):
        """Move forward N steps."""
        for i in range(steps):
            self._check()
            self.crawler.do_action("forward", 1, self.speed)
            self._wait(0.3)

    def backward(self, steps: int = 1):
        """Move backward N steps."""
        for i in range(steps):
            self._check()
            self.crawler.do_action("backward", 1, self.speed)
            self._wait(0.3)

    def turn_left(self, steps: int = 1):
        """Turn left N steps."""
        for _ in range(steps):
            self._check()
            self.crawler.do_action("turn left", 1, self.speed)
            self._wait(0.3)

    def turn_right(self, steps: int = 1):
        """Turn right N steps."""
        for _ in range(steps):
            self._check()
            self.crawler.do_action("turn right", 1, self.speed)
            self._wait(0.3)

    def push_up(self, steps: int = 1):
        """Do push-ups."""
        self.crawler.do_action("push up", steps, self.speed)
        self._wait(0.5)

    def wave(self, steps: int = 1):
        """Wave gesture."""
        self.crawler.do_action("wave", steps, self.speed)
        self._wait(0.5)

    def look_left(self, steps: int = 1):
        """Look left."""
        self.crawler.do_action("look left", steps, self.speed)
        self._wait(0.3)

    def look_right(self, steps: int = 1):
        """Look right."""
        self.crawler.do_action("look right", steps, self.speed)
        self._wait(0.3)

    def look_up(self, steps: int = 1):
        """Look up."""
        self.crawler.do_action("look up", steps, self.speed)
        self._wait(0.3)

    def look_down(self, steps: int = 1):
        """Look down."""
        self.crawler.do_action("look down", steps, self.speed)
        self._wait(0.3)

    def dance(self, steps: int = 1):
        """Dance."""
        self.crawler.do_action("dance", steps, self.speed)
        self._wait(0.5)

    def turn_left_angle(self, steps: int = 1, angle: int = 30):
        """Turn left. angle is the leg swing parameter, actual turn ~ angle x 0.44 per step."""
        self.crawler.move_list.angle = angle
        for _ in range(steps):
            self._check()
            self.crawler.do_action("turn left angle", 1, self.speed)
            self._wait(0.3)

    def turn_right_angle(self, steps: int = 1, angle: int = 30):
        """Turn right. angle is the leg swing parameter, actual turn ~ angle x 0.44 per step."""
        self.crawler.move_list.angle = angle
        for _ in range(steps):
            self._check()
            self.crawler.do_action("turn right angle", 1, self.speed)
            self._wait(0.3)

    def nod(self, steps: int = 2):
        """Nod head up and down."""
        for _ in range(steps):
            self._check()
            self.crawler.do_action("look up", 1, self.speed)
            self._wait(0.2)
            self.crawler.do_action("look down", 1, self.speed)
            self._wait(0.2)
        self.crawler.do_action("stand", 1, self.speed)
        self._wait(0.3)

    def shake_head(self, steps: int = 2):
        """Shake head side to side."""
        for _ in range(steps):
            self._check()
            self.crawler.do_action("look left", 1, self.speed)
            self._wait(0.2)
            self.crawler.do_action("look right", 1, self.speed)
            self._wait(0.2)
        self.crawler.do_action("stand", 1, self.speed)
        self._wait(0.3)

    def shake_hand(self, steps: int = 2):
        """Extend front leg for handshake."""
        self.crawler.do_action("wave", steps, self.speed)
        self._wait(0.5)

    def play_dead(self, steps: int = 1):
        """Flip over and play dead."""
        self.crawler.do_step("sit", self.speed)
        self._wait(0.5)
        self._standing = False

    def read_distance(self) -> float | None:
        """
        Read ultrasonic distance with median filter.
        Returns distance in cm, or None if read failed.
        """
        vals = []
        for _ in range(5):
            try:
                signal.alarm(1)
                d = self.sonar.read()
                signal.alarm(0)
                if d is not None and d > 0:
                    vals.append(d)
            except Timeout:
                signal.alarm(0)
            except Exception:
                signal.alarm(0)
            time.sleep(0.03)

        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    def check_obstacle(self) -> bool:
        """Returns True if obstacle detected within threshold."""
        dist = self.read_distance()
        if dist is None:
            return False
        blocked = dist <= OBSTACLE_THRESHOLD_CM
        if blocked:
            logger.info(f"Obstacle detected at {dist:.1f}cm")
        return blocked
