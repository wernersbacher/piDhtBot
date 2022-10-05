class DHTRecord:
	"""Single DHT record."""

	def __init__(self, ts=0, temp=0, hum=0):
		self.ts = ts
		self.temp = temp
		self.hum = hum

	def get(self):
		return self.temp, self.hum


class MHZRecord:
	"""Single MHZ record."""

	def __init__(self, ts=0, co2=0):
		self.ts = ts
		self.co2 = co2


class RecordCollection:
	"""Record list and corresponding record statistics."""

	class RecordStat:
		"""Record statistics."""

		def __init__(self):
			self.minTs = None
			self.maxTs = None
			self.minValue = float('inf')
			self.maxValue = -float('inf')

		def updateWithValue(self, ts, value):
			"""Update record statistics with a single value."""
			if self.minValue > value:
				self.minValue = value
				self.minTs = ts
			if self.maxValue < value:
				self.maxValue = value
				self.maxTs = ts

		def updateWithRecordStat(self, otherRecordStat):
			"""Update record statistics with another record statistics, i.e. merge them."""
			self.updateWithValue(otherRecordStat.minTs, otherRecordStat.minValue)
			self.updateWithValue(otherRecordStat.maxTs, otherRecordStat.maxValue)

	def __init__(self):
		self.recordList = []
		self.tempStat = self.RecordStat()
		self.humStat = self.RecordStat()

	def addSingleRecord(self, otherRecord):
		"""Add a single record to the record list and update the statistics."""
		self.recordList.append(otherRecord)

		self.tempStat.updateWithValue(otherRecord.ts, otherRecord.temp)
		self.humStat.updateWithValue(otherRecord.ts, otherRecord.hum)

	def addRecordList(self, otherRecords):
		"""Add a list of records and update the statistics."""
		self.recordList += otherRecords.recordList

		self.tempStat.updateWithRecordStat(otherRecords.tempStat)
		self.humStat.updateWithRecordStat(otherRecords.humStat)
