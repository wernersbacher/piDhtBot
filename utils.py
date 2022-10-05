import time


class SlowedCaller:
	""" in a fast repeating loop, call a function with slowed caller so it doesnt get called fast too."""

	def __init__(self, interval):
		"""

		@param interval: time delta where calls gets skipped if below this delta
		"""
		self.interval = interval
		self.last_run = 0

	def run(self, func):
		now = time.time()
		if now - self.interval >= self.last_run:
			self.last_run = now
			func()


class FPSleep:
	"""sleep for so long that the fps are reached"""
	def __init__(self, fps):
		self.last_update = 0
		self.max_time = 1.0/fps

	def start(self):
		self.last_update = time.time()

	def sleep(self):
		""""""
		now = time.time()
		dt = now - self.last_update
		self.last_update = now
		if 0 < dt < self.max_time:
			time.sleep(dt)  # sleep to reach the fps
			print(f"slept for {dt} seconds.")