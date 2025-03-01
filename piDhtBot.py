#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import adafruit_dht
import datetime
import json
import logging
import logging.handlers
import matplotlib.pyplot as plt
import matplotlib.ticker
import os
import re
import signal
import sys
import threading
import time
from collections import deque

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MAX_MESSAGE_LENGTH
from telegram.error import NetworkError, Unauthorized
from telegram.ext import Updater, MessageHandler, Filters, CallbackQueryHandler
import mh_z19
from Records import RecordCollection, DHTRecord, MHZRecord
from utils import SlowedCaller


class piDhtBot:
	"""Python DHT Telegram bot."""

	def __init__(self):
		# name of the bot, used as base name for logging and recording
		self.botName = 'piDhtBot'

		# config from config file
		self.config = None
		# logging stuff
		self.logger = None
		# telegram bot updater
		self.updater = None
		# are we currently shutting down?
		self.isShuttingDown = False

		# data recorder for DHT sensor
		self.recorder_dht = None
		# data recorder for MHZ sensor
		self.recorder_mhz = None
		# date time format for recorded data
		self.dateTimeFormat = '%Y-%m-%d %H:%M:%S'
		# last data record for DHT sensor
		self.lastRecordDHT: DHTRecord = DHTRecord()
		# last data record for MHZ sensor
		self.lastRecordMHZ: MHZRecord = MHZRecord()

		self.last_time_below_thres_dht = 0
		self.last_time_below_thres_mhz = 0
		self.trigger_message_sent = False

		# DHT sensor
		self.dhtDevice = None

		# path for plotted image
		self.plotImagePath = None
		# width of plotted image, in inch
		self.plotWidth = None
		# height of plotted image, in inch
		self.plotHeight = None
		# dpi of plotted image
		self.plotDPI = None

	def run(self):
		"""Run the bot and perform a cleanup afterwards."""
		try:
			self.runInternal()
		finally:
			self.cleanup()

	def runInternal(self):
		"""Run the bot."""
		# setup logging, we want to log both to stdout and a file
		logFormat = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
		logFileHandler = logging.handlers.TimedRotatingFileHandler(filename=self.botName + '.log', when='midnight',
																   backupCount=7)
		logFileHandler.setFormatter(logFormat)
		stdoutHandler = logging.StreamHandler(sys.stdout)
		stdoutHandler.setFormatter(logFormat)
		self.logger = logging.getLogger(self.botName)
		self.logger.addHandler(logFileHandler)
		self.logger.addHandler(stdoutHandler)
		self.logger.setLevel(logging.INFO)

		self.logger.info('Starting')

		# register signal handler
		signal.signal(signal.SIGHUP, self.signalHandler)
		signal.signal(signal.SIGINT, self.signalHandler)
		signal.signal(signal.SIGQUIT, self.signalHandler)
		signal.signal(signal.SIGTERM, self.signalHandler)

		try:
			self.config = json.load(open('config.json', 'r'))
		except:
			self.logger.exception('Could not parse config file:')
			sys.exit(1)

		# setup data recorder
		# use TimedRotatingFileHandler for data logging, so that we don't have to reimplement file rotation
		recordDays = int(self.config['general']['record_days'])
		recordFormat = logging.Formatter('%(message)s')

		recordFileHandlerDHT = logging.handlers.TimedRotatingFileHandler(filename=self.botName + '_dht.rec', when='midnight',
																	  backupCount=recordDays)
		recordFileHandlerDHT.setFormatter(recordFormat)
		self.recorder_dht = logging.getLogger(self.botName + 'DHTRecorder')
		self.recorder_dht.addHandler(recordFileHandlerDHT)
		self.recorder_dht.setLevel(logging.INFO)

		recordFileHandlerMHZ = logging.handlers.TimedRotatingFileHandler(filename=self.botName + '_mhz.rec', when='midnight',
																	  backupCount=recordDays)
		recordFileHandlerMHZ.setFormatter(recordFormat)
		self.recorder_mhz = logging.getLogger(self.botName + 'MHZRecorder')
		self.recorder_mhz.addHandler(recordFileHandlerMHZ)
		self.recorder_mhz.setLevel(logging.INFO)

		self.plotImagePath = self.config['plot']['path']
		self.plotDPI = int(self.config['plot']['dpi'])
		self.plotWidth = float(self.config['plot']['width'])
		self.plotHeight = float(self.config['plot']['height'])

		self.updater = Updater(self.config['telegram']['token'])
		dispatcher = self.updater.dispatcher
		bot = self.updater.bot

		# check if API access works. try again on network errors,
		# might happen after boot while the network is still being set up
		self.logger.info('Waiting for network and Telegram API to become accessible...')
		telegramAccess = False
		timeout = self.config['general']['startup_timeout']
		timeout = timeout if timeout > 0 else sys.maxsize
		for i in range(timeout):
			try:
				self.logger.info(bot.get_me())
				self.logger.info('Telegram API access working!')
				telegramAccess = True
				break  # success
			except NetworkError as e:
				pass  # don't log network errors, just ignore
			except Unauthorized as e:
				# probably wrong access token
				self.logger.exception('Error while trying to access Telegram API, wrong Telegram access token?')
				raise
			except:
				# unknown exception, log and then bail out
				self.logger.exception('Error while trying to access Telegram API:')
				raise

			time.sleep(1)

		if not telegramAccess:
			self.logger.error('Could not access Telegram API within time, shutting down')
			sys.exit(1)

		# self.send_all("Hello, I'm back online!")

		# setup dht device ONCE
		gpio = int(self.config['dht']['gpio'])
		sensor = self.config['dht']['type']
		if sensor == 'DHT11':
			self.dhtDevice = adafruit_dht.DHT11(gpio)
		elif sensor == 'DHT22':
			self.dhtDevice = adafruit_dht.DHT22(gpio)
		else:
			self.logger.error('DHT: Invalid sensor type: %s' % sensor)
			sys.exit(1)

		threads = []

		# set up DHT thread
		dht_thread = threading.Thread(target=self.readDHT, name="DHT")
		dht_thread.daemon = True
		dht_thread.start()
		threads.append(dht_thread)

		if self.config['mhz']['enabled']:
			# set up MHZ thread
			mhz_thread = threading.Thread(target=self.readMHZ, name="MHZ")
			mhz_thread.daemon = True
			mhz_thread.start()
			threads.append(mhz_thread)

		if self.config["webhook"]["enabled"]:
			# set up Webhook thread
			webhook_thread = threading.Thread(target=self.webhook_refresh, name="WebhookRefresh")
			webhook_thread.daemon = True
			webhook_thread.start()
			threads.append(webhook_thread)

		# telegram: register message handler and start polling
		# note: we don't register each command individually because then we
		# wouldn't be able to check the ownerID, instead we register for text
		# messages
		dispatcher.add_handler(CallbackQueryHandler(self.plotCallback))
		dispatcher.add_handler(MessageHandler(Filters.text, self.performCommand))
		self.updater.start_polling()

		while True:
			time.sleep(5)
			# check if all threads are still alive
			for thread in threads:
				if thread.is_alive():
					continue

				# something went wrong, bailing out
				msg = 'Thread "%s" died, terminating now.' % thread.name
				self.logger.error(msg)
				self.send_all(msg)
				sys.exit(1)

			self.check_ventilation_needed()

	def performCommand(self, update, context):
		"""Handle a received command."""
		message = update.message
		if message is None:
			return
		# skip messages from non-owner
		if message.from_user.id not in self.config['telegram']['owner_ids']:
			self.logger.warning('Received message from unknown user "%s": "%s"' % (message.from_user, message.text))
			message.reply_text("I'm sorry, Dave. I'm afraid I can't do that.")
			return

		self.logger.info('Received message from user "%s": "%s"' % (message.from_user, message.text))

		now = datetime.datetime.now()
		cmd = update.message.text.lower().rstrip()
		if cmd == '/start':
			self.commandHelp(update)
		elif cmd == '/show':
			self.commandShow(update)
		elif cmd == '/plot':
			self.commandPlot(update)
		elif cmd == '/log':
			self.commandLog(update)
		elif cmd == '/help':
			self.commandHelp(update)
		else:
			message.reply_text('Unknown command.')
			self.logger.warning('Unknown command: "%s"' % update.message.text)

	def commandHelp(self, update):
		"""Handle the help command."""
		message = update.message
		message.reply_text(
			'/show - Show last read data.\n'
			'/plot - Plot recorded data.\n'
			'/log - Show recent log messages.\n'
			'/help - Show this help.')

	def commandShow(self, update):
		"""Handle the show command. Show the last recorded data."""
		message = update.message

		text = self.create_info_string()

		message.reply_text(text)

	def commandLog(self, update):
		"""Handle the log command. Show recent log messages."""
		numLines = 100
		messages = deque(maxlen=numLines)
		fileName = self.botName + '.log'
		with open(fileName, 'r') as f:
			for line in f:
				line = line.rstrip('\n')
				messages.append(line)

		message = update.message
		message.reply_text("\n".join(messages)[-MAX_MESSAGE_LENGTH:])

	def commandPlot(self, update):
		"""Handle the plot command. Present time ranges to the user."""
		message = update.message

		options = [
			[
				InlineKeyboardButton("1h", callback_data='1h'),
				InlineKeyboardButton("3h", callback_data='3h'),
				InlineKeyboardButton("6h", callback_data='6h'),
				InlineKeyboardButton("12h", callback_data='12h'),
				InlineKeyboardButton("24h", callback_data='24h'),
				InlineKeyboardButton("48h", callback_data='48h'),
			],
			[
				InlineKeyboardButton("today", callback_data='today'),
				InlineKeyboardButton("yesterday", callback_data='yesterday'),
				InlineKeyboardButton("last 3 days", callback_data='last 3d'),
			],
			[
				InlineKeyboardButton("this week", callback_data='this week'),
				InlineKeyboardButton("last week", callback_data='last week'),
				InlineKeyboardButton("last 7 days", callback_data='last 7d'),
			],
			[
				InlineKeyboardButton("this month", callback_data='this month'),
				InlineKeyboardButton("last month", callback_data='last month'),
				InlineKeyboardButton("last 31 days", callback_data='last 31d'),
			],
			[
				InlineKeyboardButton("this year", callback_data='this year'),
				InlineKeyboardButton("last year", callback_data='last year'),
				InlineKeyboardButton("last 365 days", callback_data='last 365d'),
			],
			[InlineKeyboardButton("all", callback_data='all')],
		]

		reply = InlineKeyboardMarkup(options)
		update.message.reply_text('Choose time range to plot:', reply_markup=reply)

	def plotCallback(self, update, context):
		"""Callback for the plot command. The user chose a time range to plot."""
		query = update.callback_query

		# CallbackQueries need to be answered, even if no notification to the user is needed
		# Some clients may have trouble otherwise. See https://core.telegram.org/bots/api#callbackquery
		query.answer()

		message = query.message

		if query.data == 'all':
			self.plot(message)
			return

		now = datetime.datetime.now()

		# 1h, 3h, 6h, ...
		hours = re.search('^([0-9]+)h$', query.data)
		if hours:
			number = int(hours.group(1))
			dateStart = now - datetime.timedelta(hours=number)
			self.plot(message, dateStart)
			return

		todayStart = now.replace(hour=0, minute=0, second=0, microsecond=0)

		# last 3d, last 7d, ...
		days = re.search('^last ([0-9]+)d$', query.data)
		if days:
			number = int(days.group(1))
			dateStart = todayStart - datetime.timedelta(days=number - 1)
			self.plot(message, dateStart)
			return

		if query.data == 'today':
			dateStart = todayStart
			self.plot(message, dateStart)
			return
		elif query.data == 'yesterday':
			dateStart = todayStart - datetime.timedelta(days=1)
			dateEnd = todayStart - datetime.timedelta(microseconds=1)
			self.plot(message, dateStart, dateEnd)
			return
		elif query.data == 'this week':
			dateStart = todayStart - datetime.timedelta(days=now.weekday())
			self.plot(message, dateStart)
			return
		elif query.data == 'last week':
			dateStart = todayStart - datetime.timedelta(days=now.weekday() + 7)
			dateEnd = todayStart - datetime.timedelta(days=now.weekday(), microseconds=1)
			self.plot(message, dateStart, dateEnd)
			return
		elif query.data == 'this month':
			dateStart = todayStart - datetime.timedelta(days=now.day - 1)
			self.plot(message, dateStart)
			return
		elif query.data == 'last month':
			dateStart = todayStart - datetime.timedelta(days=todayStart.day)
			dateStart -= datetime.timedelta(days=dateStart.day - 1)
			dateEnd = todayStart - datetime.timedelta(days=todayStart.day - 1, microseconds=1)
			self.plot(message, dateStart, dateEnd)
			return
		elif query.data == 'this year':
			dateStart = todayStart.replace(month=1, day=1)
			self.plot(message, dateStart)
			return
		elif query.data == 'last year':
			dateStart = todayStart.replace(year=todayStart.year - 1, month=1, day=1)
			dateEnd = todayStart.replace(month=1, day=1) - datetime.timedelta(microseconds=1)
			self.plot(message, dateStart, dateEnd)
			return
		else:
			message.reply_text('Unknown plot range: %s' % query.data)

	def plot(self, message, dateStart=None, dateEnd=None):
		"""Plot the selected time range and send the plot to the user."""
		msg = 'Plotting from %s to %s.' % (
			dateStart.replace(microsecond=0) if dateStart is not None else 'start',
			dateEnd.replace(microsecond=0) if dateEnd is not None else 'now')
		self.logger.info(msg)
		message.reply_text(msg)

		recordBaseName = self.botName + '_dht.rec'
		records = self.getRecords(recordBaseName, dateStart, dateEnd)
		if len(records.recordList) == 0:
			message.reply_text('No data for this time range.')
			return

		self.plotRecords(records.recordList)

		dateStart = records.recordList[0].ts
		dateEnd = records.recordList[-1].ts
		caption = (
				'From %s to %s\n'
				'Temperature:\n'
				'  Minimum: %.2f °C at %s\n'
				'  Maximum: %.2f °C at %s\n'
				'Humidity:\n'
				'  Minimum: %.2f %% at %s\n'
				'  Maximum: %.2f %% at %s' % (
					dateStart.replace(microsecond=0), dateEnd.replace(microsecond=0),
					records.tempStat.minValue, records.tempStat.minTs,
					records.tempStat.maxValue, records.tempStat.maxTs,
					records.humStat.minValue, records.humStat.minTs,
					records.humStat.maxValue, records.humStat.maxTs))
		message.reply_photo(photo=open(self.plotImagePath, 'rb'), caption=caption)

		os.remove(self.plotImagePath)

	def addRecord(self, record):
		"""Add a single record."""
		ts = record.ts.strftime(self.dateTimeFormat)
		if isinstance(record, DHTRecord):
			self.recorder_dht.info('%s %.2f %.2f' % (ts, record.temp, record.hum))
		elif isinstance(record, MHZRecord):
			self.recorder_mhz.info('%s %.2f' % (ts, record.co2))

	def getRecords(self, recordBaseName, dateStart=None, dateEnd=None):
		"""Get records for the given time range."""
		allRecords = RecordCollection()
		for recordFile in self.listRecordFiles(recordBaseName, dateStart, dateEnd):
			records = self.readRecords(recordFile, dateStart, dateEnd)
			if len(records.recordList) == 0:
				continue

			allRecords.addRecordList(records)
		return allRecords

	def listRecordFiles(self, recordBaseName, dateStart=None, dateEnd=None):
		"""List all record files."""
		if dateStart is not None:
			# replace with start of the day since data records are stored per day
			dateStart = dateStart.replace(hour=0, minute=0, second=0, microsecond=0)
		recordFiles = []
		files = os.listdir('.')
		files.sort()
		for fileName in files:
			if not fileName.startswith(recordBaseName):
				continue
			if fileName == recordBaseName:
				# skip current record
				continue
			try:
				dateStr = fileName[len(recordBaseName) + 1:]
				fileDate = datetime.datetime.strptime(dateStr, '%Y-%m-%d')
			except:
				self.logger.exception('Error: Parsing date from record file %s failed:' % fileName)
				continue
			if dateStart is not None and fileDate < dateStart:
				continue
			if dateEnd is not None and fileDate > dateEnd:
				continue
			recordFiles.append(fileName)
		recordFiles.sort()
		# add current record always at the end
		recordFiles.append(recordBaseName)
		return recordFiles

	def readRecords(self, fileName, dateStart=None, dateEnd=None):
		"""Read the given record file and return records for the given time range."""
		with open(fileName, 'r') as f:
			records = RecordCollection()
			lineNum = 0
			for line in f:
				lineNum += 1
				line = line.rstrip('\n')
				try:
					date, time, temp, hum = line.split()
					ts = datetime.datetime.strptime('%s %s' % (date, time), self.dateTimeFormat)
					if dateStart is not None and ts < dateStart:
						continue
					if dateEnd is not None and ts > dateEnd:
						continue
					temp = float(temp)
					hum = float(hum)
				except:
					self.logger.exception(
						'Error: Parsing of data record (file %s, line %d) failed:' % (fileName, lineNum))
					continue

				records.addSingleRecord(DHTRecord(ts, temp, hum))
			return records

	def plotRecords(self, records):
		"""Plot the given records."""
		if self.plotWidth is not None and self.plotHeight is not None:
			figsize = (self.plotWidth, self.plotHeight)
		else:
			figsize = None
		fig, ax1 = plt.subplots(figsize=figsize, dpi=self.plotDPI)
		ax2 = ax1.twinx()
		# time and temperature
		ax1.set_ylabel('Temperature in °C', color='red')
		ax1.tick_params('y', colors='red')
		ax1.plot([r.ts for r in records], [r.temp for r in records], color='red')
		# time and humidity
		ax2.set_ylabel('Humidity in %', color='blue')
		ax2.tick_params('y', colors='blue')
		ax2.plot([r.ts for r in records], [r.hum for r in records], color='blue')

		# align ticks between ax1 and ax2,
		# from https://stackoverflow.com/a/45052300/1340631
		l1 = ax1.get_ylim()
		l2 = ax2.get_ylim()
		f = lambda x: l2[0] + (x - l1[0]) / (l1[1] - l1[0]) * (l2[1] - l2[0])
		ticks = f(ax1.get_yticks())
		ax2.yaxis.set_major_locator(matplotlib.ticker.FixedLocator(ticks))

		ax1.grid()
		plt.gcf().autofmt_xdate()
		plt.tight_layout()

		plt.savefig(self.plotImagePath)

	def check_ventilation_needed(self):
		""" checks if values below threshold for a longer time so it sends a message"""

		if not self.config['general']['enable_ventilation_checker']:
			return

		now = time.time()
		mhz_enabled = self.config['mhz']['enabled']

		trigger_message = False

		try:
			dht_thres = self.config['dht']['thres']
			dht_time = self.config['dht']['thres_time_passed']
		except KeyError:
			self.logger.error("No DHT threshold config found for ventilation checker!")
			return

		last_hum = self.lastRecordDHT.hum
		if last_hum < dht_thres:
			# if below thres, reset timestamp.
			self.last_time_below_thres_dht = now
		elif now - self.last_time_below_thres_dht >= dht_time:
			# check if threshold is above value for a long time so we trigger a message
			trigger_message = True

		if mhz_enabled:
			try:
				mhz_thres = self.config['mhz']['thres']
				mhz_time = self.config['mhz']['thres_time_passed']
			except KeyError:
				self.logger.error("No MHZ threshold config found for ventilation checker!")
				return
			last_co2 = self.lastRecordMHZ.co2
			if last_co2 < mhz_thres:
				self.last_time_below_thres_mhz = now
			elif now - self.last_time_below_thres_mhz >= mhz_time:
				# above thres for a long time, send message please!
				trigger_message = True

		if trigger_message and not self.trigger_message_sent:
			self.logger.info("Sending warning message because of threshold values!")
			message = "ATTENTION! You should open some windows!\n"
			message += self.create_info_string()
			self.send_all(message)
			# stop sending the message again
			self.trigger_message_sent = True
		elif not trigger_message:
			# below thresholds so we are allowed to send a message again!
			self.trigger_message_sent = False

	def send_all(self, message):
		bot = self.updater.bot
		ownerIDs = self.config['telegram']['owner_ids']
		for ownerID in ownerIDs:
			try:
				bot.sendMessage(chat_id=ownerID, text=message)
			except:
				# most likely network problem or user has blocked the bot
				self.logger.exception('Could not send message to user %s:' % ownerID)

	def create_info_string(self):
		if self.lastRecordDHT.hum == 0:
			return 'No data yet.'

		recordDHT = self.lastRecordDHT

		output = f'{recordDHT.ts.replace(microsecond=0)}\n' \
				 f'Temperature: {recordDHT.temp:.2f} °C\n' \
				 f'Humidity: {recordDHT.hum:.2f} %\n'

		if self.config['mhz']['enabled']:
			recordMHZ = self.lastRecordMHZ
			output += f"CO2: {recordMHZ.co2} ppm"

		return output

	def webhook_refresh(self):
		"""sends current state to configured webhook
		todo: exit gracefully?
		"""
		self.logger.info('Setting up Webhook thread')
		now = time.time()
		webhook_interval = self.config["webhook"]["interval"]
		webhook_multi = self.config["webhook"]["multi"]

		url = self.config["webhook"]["url"]

		while True:
			temp, hum = self.lastRecordDHT.get()
			co2 = self.lastRecordMHZ.co2

			if hum == 0:
				time.sleep(0.2)  # wait for first real data
				continue

			formatted_url = url.format(temp*webhook_multi, hum*webhook_multi, co2*webhook_multi)
			try:
				requests.get(formatted_url)
			except requests.exceptions.RequestException as e:
				self.logger.warning(f"Could not update webhook URL: {formatted_url} \n {e}")

			nextRead = now + webhook_interval
			now = time.time()
			if now < nextRead:
				time.sleep(nextRead - now)

	def readDHT(self):
		"""Continuously read from the DHT sensor."""
		self.logger.info('Setting up DHT thread')

		# minimum read interval is 2.0, lower intervals will return cached values
		readInterval = max(2.0, float(self.config['dht']['read_interval']))

		# add gap marker
		now = datetime.datetime.now()
		record = DHTRecord(now, float('NaN'), float('NaN'))
		self.addRecord(record)

		firstRead = True
		nextRead = lastComplain = datetime.datetime.now()
		while True:
			now = datetime.datetime.now()
			# complain if we can't fulfill our read interval, but not too often
			if now > (nextRead + datetime.timedelta(seconds=readInterval)) and \
					(now - lastComplain).total_seconds() > 60 * 5:
				self.logger.warning('DHT: Could not read from sensor within time')
				lastComplain = now
				# add gap marker
				record = DHTRecord(now, float('NaN'), float('NaN'))
				self.addRecord(record)

			try:
				temp = self.dhtDevice.temperature
				hum = self.dhtDevice.humidity
			except RuntimeError as e:
				# errors are expected when reading from DHT, only log them in debug mode
				self.logger.debug('DHT: Received exception: %s' % e)
				time.sleep(0.2)
				continue
			except:
				# unknown exception
				self.logger.exception('DHT: Unknown exception received')
				# don't continue here, see e.g. https://github.com/adafruit/Adafruit_CircuitPython_DHT/issues/50
				self.dhtDevice.exit()
				raise

			if temp is None or hum is None:
				self.logger.debug('DHT: Read incomplete data, trying again')
				time.sleep(0.2)
				continue

			temp = (temp + self.config["dht"]["offset_temp"]) * self.config["dht"]["scale_temp"]
			hum = (hum + self.config["dht"]["offset_hum"]) * self.config["dht"]["scale_hum"]

			# read from sensor succeeded
			if firstRead:
				firstRead = False
				self.logger.info('DHT: Sensor working')
			try:
				record = DHTRecord(now, temp, hum)
				self.lastRecordDHT = record
				self.addRecord(record)
			except:
				self.logger.exception('DHT: Failed to create record')

			nextRead += datetime.timedelta(seconds=readInterval)
			now = datetime.datetime.now()
			if now < nextRead:
				time.sleep((nextRead - now).total_seconds())

	def readMHZ(self):
		self.logger.info('Setting up MHZ thread')

		# add gap marker
		now = datetime.datetime.now()
		record = DHTRecord(now, float('NaN'), float('NaN'))
		self.addRecord(record)
		firstRead = True
		nextRead = lastComplain = datetime.datetime.now()

		readInterval = float(self.config['mhz']['read_interval'])

		while True:
			now = datetime.datetime.now()
			# complain if we can't fulfill our read interval, but not too often
			if now > (nextRead + datetime.timedelta(seconds=readInterval)) and \
					(now - lastComplain).total_seconds() > 60 * 5:
				self.logger.warning('MHZ: Could not read from sensor within time')
				lastComplain = now
				# add gap marker
				record = DHTRecord(now, float('NaN'), float('NaN'))
				self.addRecord(record)

			try:
				reading = mh_z19.read()
				co2 = reading["co2"]
			except:
				# unknown exception
				self.logger.exception('MHZ: Unknown exception received')
				raise

			# read from sensor succeeded
			if firstRead:
				firstRead = False
				self.logger.info('MHZ: Sensor working')
			try:
				record = MHZRecord(now, co2)
				self.lastRecordMHZ = record
				self.addRecord(record)
			except:
				self.logger.exception('MHZ: Failed to create record')

			nextRead += datetime.timedelta(seconds=readInterval)
			now = datetime.datetime.now()
			if now < nextRead:
				time.sleep((nextRead - now).total_seconds())

	def cleanup(self):
		"""Cleanup, to be called on program termination."""
		if self.dhtDevice is not None:
			try:
				self.logger.info('Cleaning up DHT device')
				self.dhtDevice.exit()
			except:
				pass

		if self.updater is not None and self.updater.running:
			try:
				self.logger.info('Stopping telegram updater')
				self.updater.stop()
			except:
				pass

		self.logger.info('Cleanup done')

	def signalHandler(self, signal, frame):
		"""Signal handler."""
		# prevent multiple calls by different signals (e.g. SIGHUP, then SIGTERM)
		if self.isShuttingDown:
			return
		self.isShuttingDown = True

		msg = 'Caught signal %d, terminating now.' % signal
		self.logger.error(msg)

		# try to inform owners
		if self.updater and self.updater.running:
			try:
				bot = self.updater.dispatcher.bot
				for ownerID in self.config['telegram']['owner_ids']:
					try:
						bot.sendMessage(chat_id=ownerID, text=msg)
					except:
						pass
			except:
				pass

		sys.exit(1)


if __name__ == '__main__':
	bot = piDhtBot()
	bot.run()
