# vim: ts=8:sts=8:sw=8:noexpandtab
#
# This file is part of ReText
# Copyright: 2015 Dmitry Shachnev
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from markups import get_markup_for_file_name, find_markup_class_by_name
from markups.common import MODULE_HOME_PAGE

from ReText import app_version, globalSettings, converterprocess
from ReText.editor import ReTextEdit
from ReText.highlighter import ReTextHighlighter

try:
	import enchant
except ImportError:
	enchant = None

from PyQt5.QtCore import pyqtSignal, Qt, QDir, QFile, QFileInfo, QPoint, QTextStream, QTimer, QUrl
from PyQt5.QtGui import QDesktopServices, QTextCursor, QTextDocument
from PyQt5.QtWidgets import QTextBrowser, QTextEdit, QSplitter

try:
	from ReText.webkitpreview import ReTextWebPreview
except ImportError:
	ReTextWebPreview = None

PreviewDisabled, PreviewLive, PreviewNormal = range(3)

class ReTextTab(QSplitter):

	fileNameChanged = pyqtSignal()
	modificationStateChanged = pyqtSignal()
	activeMarkupChanged = pyqtSignal()

	# Make _fileName a read-only property to make sure that any
	# modification happens through the proper functions. These functions
	# will make sure that the fileNameChanged signal is emitted when
	# applicable.
	@property
	def fileName(self):
		return self._fileName

	def __init__(self, parent, fileName, previewState=PreviewDisabled):
		super(QSplitter, self).__init__(Qt.Horizontal, parent=parent)
		self.p = parent
		self._fileName = fileName
		self.editBox = ReTextEdit(self)
		self.previewBox = self.createPreviewBox(self.editBox)
		self.activeMarkupClass = None
		self.markup = None
		self.converted = None
		self.previewState = previewState
		self.previewOutdated = False
		self.conversionPending = False

		self.converterProcess = converterprocess.ConverterProcess()
		self.converterProcess.conversionDone.connect(self.updatePreviewBox)

		textDocument = self.editBox.document()
		self.highlighter = ReTextHighlighter(textDocument)
		if enchant is not None and parent.actionEnableSC.isChecked():
			self.highlighter.dictionary = enchant.Dict(parent.sl or None)
			# Rehighlighting is tied to the change in markup class that
			# happens at the end of this function

		self.editBox.textChanged.connect(self.triggerPreviewUpdate)
		self.editBox.undoAvailable.connect(parent.actionUndo.setEnabled)
		self.editBox.redoAvailable.connect(parent.actionRedo.setEnabled)
		self.editBox.copyAvailable.connect(parent.actionCopy.setEnabled)

		# Give both boxes a minimum size so the minimumSizeHint will be
		# ignored when splitter.setSizes is called below
		for widget in self.editBox, self.previewBox:
			widget.setMinimumWidth(125)
			self.addWidget(widget)
		self.setSizes((50, 50))
		self.setChildrenCollapsible(False)

		textDocument.modificationChanged.connect(self.handleModificationChanged)

		self.updateActiveMarkupClass()

	def handleModificationChanged(self):
		self.modificationStateChanged.emit()

	def createPreviewBox(self, editBox):

		# Use closures to avoid a hard reference from ReTextWebPreview
		# to self, which would keep the tab and its resources alive
		# even after other references to it have disappeared.

		def editorPositionToSourceLine(editorPosition):
			viewportPosition = editorPosition - editBox.verticalScrollBar().value()
			sourceLine = editBox.cursorForPosition(QPoint(0,viewportPosition)).blockNumber()
			return sourceLine

		def sourceLineToEditorPosition(sourceLine):
			doc = editBox.document()
			block = doc.findBlockByNumber(sourceLine)
			rect = doc.documentLayout().blockBoundingRect(block)
			return rect.top()

		if globalSettings.useWebKit:
			preview = ReTextWebPreview(editBox,
			                           editorPositionToSourceLine,
			                           sourceLineToEditorPosition)
		else:
			preview = ReTextPreview(self)

		return preview

	def getActiveMarkupClass(self):
		'''
		Return the currently active markup class for this tab.
		No objects should be created of this class, it should
		only be used to retrieve markup class specific information.
		'''
		return self.activeMarkupClass

	def updateActiveMarkupClass(self):
		'''
		Update the active markup class based on the default class and
		the current filename. If the active markup class changes, the
		highlighter is rerun on the input text, the markup object of
		this tab is replaced with one of the new class and the
		activeMarkupChanged signal is emitted.
		'''
		previousMarkupClass = self.activeMarkupClass

		self.activeMarkupClass = find_markup_class_by_name(globalSettings.defaultMarkup)

		if self._fileName:
			markupClass = get_markup_for_file_name(
				self._fileName, return_class=True)
			if markupClass:
				self.activeMarkupClass = markupClass

		if self.activeMarkupClass != previousMarkupClass:
			self.highlighter.docType = self.activeMarkupClass.name if self.activeMarkupClass else None
			self.highlighter.rehighlight()

			self.activeMarkupChanged.emit()
			self.triggerPreviewUpdate()

	def getDocumentTitleFromConverted(self, converted):
		if converted:
			try:
				return converted.get_document_title()
			except Exception:
				self.p.printError()

		return self.getBaseName()

	def getBaseName(self):
		if self._fileName:
			fileinfo = QFileInfo(self._fileName)
			basename = fileinfo.completeBaseName()
			return (basename if basename else fileinfo.fileName())
		return self.tr("New document")

	def getHtmlFromConverted(self, converted, includeStyleSheet=True, webenv=False):
		if converted is None:
			markupClass = self.getActiveMarkupClass()
			errMsg = self.tr('Could not parse file contents, check if '
			                 'you have the <a href="%s">necessary module</a> '
			                 'installed!')
			try:
				errMsg %= markupClass.attributes[MODULE_HOME_PAGE]
			except (AttributeError, KeyError):
				# Remove the link if markupClass doesn't have the needed attribute
				errMsg = errMsg.replace('<a href="%s">', '').replace('</a>', '')
			return '<p style="color: red">%s</p>' % errMsg
		headers = ''
		if includeStyleSheet:
			headers += '<style type="text/css">\n' + self.p.ss + '</style>\n'
		baseName = self.getBaseName()
		cssFileName = baseName + '.css'
		if QFile.exists(cssFileName):
			headers += ('<link rel="stylesheet" type="text/css" href="%s">\n'
			% cssFileName)
		headers += ('<meta name="generator" content="ReText %s">\n' % app_version)
		return converted.get_whole_html(
			custom_headers=headers, include_stylesheet=includeStyleSheet,
			fallback_title=baseName, webenv=webenv)

	def getDocumentForExport(self, includeStyleSheet, webenv):
		markupClass = self.getActiveMarkupClass()
		if markupClass and markupClass.available():
			exportMarkup = markupClass(filename=self._fileName)

			text = self.editBox.toPlainText()
			converted = exportMarkup.convert(text)
		else:
			converted = None

		return (self.getDocumentTitleFromConverted(converted),
		        self.getHtmlFromConverted(converted, includeStyleSheet=includeStyleSheet, webenv=webenv),
			self.previewBox)

	def updatePreviewBox(self):
		self.conversionPending = False

		try:
			self.converted = self.converterProcess.get_result()
		except converterprocess.MarkupNotAvailableError:
			self.converted = None
		except converterprocess.ConversionError:
			return self.p.printError()

		if isinstance(self.previewBox, QTextEdit):
			scrollbar = self.previewBox.verticalScrollBar()
			scrollbarValue = scrollbar.value()
			distToBottom = scrollbar.maximum() - scrollbarValue
		try:
			html = self.getHtmlFromConverted(self.converted)
		except Exception:
			return self.p.printError()
		if isinstance(self.previewBox, QTextEdit):
			self.previewBox.setHtml(html)
			self.previewBox.document().setDefaultFont(globalSettings.font)
			# If scrollbar was at bottom (and that was not the same as top),
			# set it to bottom again
			if scrollbarValue:
				newValue = scrollbar.maximum() - distToBottom
				scrollbar.setValue(newValue)
		else:
			self.previewBox.updateFontSettings()

			# Always provide a baseUrl otherwise QWebView will
			# refuse to show images or other external objects
			if self._fileName:
				baseUrl = QUrl.fromLocalFile(self._fileName)
			else:
				baseUrl = QUrl.fromLocalFile(QDir.currentPath())
			self.previewBox.setHtml(html, baseUrl)

		if self.previewOutdated:
			self.triggerPreviewUpdate()

	def triggerPreviewUpdate(self):
		self.previewOutdated = True

		if not self.conversionPending:
			self.conversionPending = True
			QTimer.singleShot(500, self.startPendingConversion)

	def startPendingConversion(self):
			self.previewOutdated = False

			requested_extensions = ['ReText.mdx_posmap'] if globalSettings.syncScroll else []
			self.converterProcess.start_conversion(self.getActiveMarkupClass().name,
			                                       self.fileName,
							       requested_extensions,
							       self.editBox.toPlainText())

	def updateBoxesVisibility(self):
		self.editBox.setVisible(self.previewState < PreviewNormal)
		self.previewBox.setVisible(self.previewState > PreviewDisabled)

	def detectFileEncoding(self, fileName):
		'''
		Detect content encoding of specific file.

		It will return None if it can't determine the encoding.
		'''
		try:
			import chardet
		except ImportError:
			return

		with open(fileName, 'rb') as inputFile:
			raw = inputFile.read(2048)

		result = chardet.detect(raw)
		if result['confidence'] > 0.9:
			if result['encoding'].lower() == 'ascii':
				# UTF-8 files can be falsely detected as ASCII files if they
				# don't contain non-ASCII characters in first 2048 bytes.
				# We map ASCII to UTF-8 to avoid such situations.
				return 'utf-8'
			return result['encoding']

	def readTextFromFile(self, fileName=None, encoding=None):
		previousFileName = self._fileName
		if fileName:
			self._fileName = fileName

		# Only try to detect encoding if it is not specified
		if encoding is None and globalSettings.detectEncoding:
			encoding = self.detectFileEncoding(self._fileName)

		# TODO: why do we open the file twice: for detecting encoding
		# and for actual read? Can we open it just once?
		openfile = QFile(self._fileName)
		openfile.open(QFile.ReadOnly)
		stream = QTextStream(openfile)
		encoding = encoding or globalSettings.defaultCodec
		if encoding:
			stream.setCodec(encoding)
			# If encoding is specified or detected, we should save the file with
			# the same encoding
			self.editBox.document().setProperty("encoding", encoding)

		text = stream.readAll()
		openfile.close()

		self.editBox.setPlainText(text)
		self.editBox.document().setModified(False)

		if previousFileName != self._fileName:
			self.updateActiveMarkupClass()
			self.fileNameChanged.emit()

	def writeTextToFile(self, fileName=None):
		# Just writes the text to file, without any changes to tab object
		# Used directly for i.e. export extensions

		# Get text from the cursor to avoid tweaking special characters,
		# see https://bugreports.qt.io/browse/QTBUG-57552 and
		# https://github.com/retext-project/retext/issues/216
		cursor = self.editBox.textCursor()
		cursor.select(QTextCursor.Document)
		text = cursor.selectedText().replace('\u2029', '\n')

		savefile = QFile(fileName or self._fileName)
		result = savefile.open(QFile.WriteOnly)
		if result:
			savestream = QTextStream(savefile)

			# Save the file with original encoding
			encoding = self.editBox.document().property("encoding")
			if encoding is not None:
				savestream.setCodec(encoding)

			savestream << text
			savefile.close()
		return result

	def saveTextToFile(self, fileName=None):
		# Sets fileName as tab fileName and writes the text to that file
		if self._fileName:
			self.p.fileSystemWatcher.removePath(self._fileName)
		result = self.writeTextToFile(fileName)
		if result:
			self.editBox.document().setModified(False)
			self.p.fileSystemWatcher.addPath(fileName or self._fileName)
			if fileName and self._fileName != fileName:
				self._fileName = fileName
				self.updateActiveMarkupClass()
				self.fileNameChanged.emit()

		return result

	def find(self, text, flags, replaceText=None, wrap=False):
		cursor = self.editBox.textCursor()
		if wrap and flags & QTextDocument.FindBackward:
			cursor.movePosition(QTextCursor.End)
		elif wrap:
			cursor.movePosition(QTextCursor.Start)
		if replaceText is not None and cursor.selectedText() == text:
			newCursor = cursor
		else:
			newCursor = self.editBox.document().find(text, cursor, flags)
		if not newCursor.isNull():
			if replaceText is not None:
				newCursor.insertText(replaceText)
				newCursor.movePosition(QTextCursor.Left, QTextCursor.MoveAnchor, len(replaceText))
				newCursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, len(replaceText))
			self.editBox.setTextCursor(newCursor)
			return True
		if not wrap:
			return self.find(text, flags, replaceText, True)
		return False

	def replaceAll(self, text, replaceText):
		cursor = self.editBox.textCursor()
		cursor.beginEditBlock()
		cursor.movePosition(QTextCursor.Start)
		flags = QTextDocument.FindFlags()
		cursor = lastCursor = self.editBox.document().find(text, cursor, flags)
		while not cursor.isNull():
			cursor.insertText(replaceText)
			lastCursor = cursor
			cursor = self.editBox.document().find(text, cursor, flags)
		if not lastCursor.isNull():
			lastCursor.movePosition(QTextCursor.Left, QTextCursor.MoveAnchor, len(replaceText))
			lastCursor.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor, len(replaceText))
			self.editBox.setTextCursor(lastCursor)
		self.editBox.textCursor().endEditBlock()
		return not lastCursor.isNull()

class ReTextPreview(QTextBrowser):
	"""
	When links like [test](test) are clicked, the file test.md is opened.
	It has to be located next to the current opened file.
	Relative pathes like [test](../test) or [test](folder/test) are also possible.
	"""

	def __init__(self, tab):
		QTextBrowser.__init__(self)
		self.tab = tab
		# if set to True, links to other files will unsuccessfully be opened as anchors
		self.setOpenLinks(False)
		self.anchorClicked.connect(self.openInternal)

	def disconnectExternalSignals(self):
		pass

	def openInternal(self, link):
		url = link.url()
		isLocalHtml = (link.scheme() in ('file', '') and url.endswith('.html'))
		if url.startswith('#'):
			self.scrollToAnchor(url[1:])
		elif link.isRelative() and get_markup_for_file_name(url, return_class=True):
			fileToOpen = QDir.current().filePath(url)
			if not QFileInfo(fileToOpen).completeSuffix() and self._fileName:
				fileToOpen += '.' + QFileInfo(self.tab.fileName).completeSuffix()
			self.tab.p.openFileWrapper(fileToOpen)
		elif globalSettings.handleWebLinks and isLocalHtml:
			self.setSource(link)
		else:
			QDesktopServices.openUrl(link)
