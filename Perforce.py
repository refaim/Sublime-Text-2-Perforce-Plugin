# TODO: add option to show N context lines in diff
# TODO: show all errors in panel
# TODO: comment all methods
# TODO: justify changelist numbers in 'submit' command

# Written by Eric Martel (emartel@gmail.com / www.ericmartel.com)

# Available commands are listed in Default.sublime-commands

# changelog
# Eric Martel - first implementation of add / checkout
# Tomek Wytrebowicz & Eric Martel - handling of forward slashes in clientspec folder
# Rocco De Angelis & Eric Martel - first implementation of revert
# Eric Martel - first implementation of diff
# Eric Martel - first implementation of Graphical Diff from Depot
# Eric Martel - first pass on changelist manipulation
# Eric Martel - first implementation for rename / delete & added on_modified as a condition to checkout a file
# Jan van Valburg -  bug fix for better support of client workspaces
# Eric Martel - better handling of clientspecs
# Rocco De Angelis - parameterized graphical diff
# Eric Martel & adecold - only list pending changelists belonging to the current user
# Eric Martel - source bash_profile when calling p4 on Mac OSX
# Eric Martel - first implementation of submit
# Eric Martel - Better handling of P4CONFIG files
# Andrew Butt & Eric Martel - threading of the diff task and selector for the graphical diff application

import sublime
import sublime_plugin

import functools
import json
import os
import re
import subprocess
import tempfile
import threading
import unittest

# Plugin Settings are located in 'perforce.sublime-settings' make a copy in the User folder to keep changes

# Executed at startup to store the path of the plugin... necessary to open files relative to the plugin
perforceplugin_dir = os.getcwdu()

PERFORCE_SETTINGS_PATH = 'Perforce.sublime-settings'
PERFORCE_ENVIRONMENT_VARIABLES = ('P4PORT', 'P4CLIENT', 'P4USER', 'P4PASSWD')
PERFORCE_DEFAULT_DESCRIPTION = '<enter description here>'

PERFORCE_P4_ERROR_PREFIX = 'error'
PERFORCE_P4_OUTPUT_PREFIXES = (
    PERFORCE_P4_ERROR_PREFIX,
    'info',
    'info1',
    'info2',
    'text',
)

PERFORCE_P4_DIFF_HEADER_RE = re.compile(r'^={4}.+={4}$')

PERFORCE_P4_CHANGES_CL_RE = re.compile(
    r'''^Change\s
       (?P<number>\d+)\son\s
       (?P<date>\S+)\sby\s
       (?P<author>\S+)\s\*pending\*\s
       '(?P<description>.+)'$
    ''', re.VERBOSE | re.MULTILINE)

PERFORCE_P4_CLIENT_ERROR_MESSAGE = 'Perforce client error'

PERFORCE_P4_OUTPUT_START_MESSAGE = 'P4 OUTPUT START'
PERFORCE_P4_OUTPUT_END_MESSAGE = 'P4 OUTPUT END'

PERFORCE_P4_NO_OPENED_FILES_ERROR = 'File(s) not opened on this client.'


def load_settings():
    return sublime.load_settings(PERFORCE_SETTINGS_PATH)


def save_settings(settings):
    settings.save(PERFORCE_SETTINGS_PATH)


def main_thread(callback, *args, **kwargs):
    # sublime.set_timeout gets used to send things onto the main thread
    # most sublime.[something] calls need to be on the main thread
    sublime.set_timeout(functools.partial(callback, *args, **kwargs), 0)


def display_message(message):
    main_thread(sublime.status_message, message)


def is_writable(path):
    return os.access(path, os.W_OK)


class ThreadProgress(object):
    def __init__(self, thread, message):
        self.thread = thread
        self.message = message
        self.addend = 1
        self.size = 8
        sublime.set_timeout(lambda: self.run(0), 100)

    def run(self, i):
        if not self.thread.is_alive():
            # TODO: eventually this message overwrite other
            # significant messages set from main thread.
            sublime.status_message(self.message)
            return

        before = i % self.size
        after = (self.size - 1) - before
        sublime.status_message('%s [%s=%s]' % \
            (self.message, ' ' * before, ' ' * after))
        if not before:
            self.addend = 1
        elif not after:
            self.addend = -1
        i += self.addend
        sublime.set_timeout(lambda: self.run(i), 100)


class CommandThread(threading.Thread):
    def __init__(self, command, on_done, **kwargs):
        threading.Thread.__init__(self)
        self.command = command
        self.on_done = on_done
        self.stdin = kwargs.get('stdin', None)
        self.env = kwargs['env']
        self.cwd = kwargs['cwd']

    def run(self):
        # Workaround for http://bugs.python.org/issue8557
        shell = sublime.platform() == 'windows'

        process = subprocess.Popen(self.command,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE, cwd=self.cwd, env=self.env,
            shell=shell, universal_newlines=True)
        output = process.communicate(self.stdin)[0] or ''
        main_thread(self.on_done, output, process.returncode)


class PerforceCommand(object):
    def run_command(self, command, callback=None, **kwargs):
        # TODO: what if p4 is not in path?
        raw_command = ['p4', '-s'] + command
        self.command = ' '.join(raw_command)
        message = kwargs.get('status_message', self.command)

        self.allowed_errors = kwargs.get('allowed_errors', [])
        self.verbose = kwargs.get('verbose', False)

        # If cwd is not passed, use directory of the current file.
        kwargs.setdefault('cwd',
            os.path.dirname(self.active_view().file_name()))

        if sublime.platform == 'osx':
            raw_command = ['source', '~/.bash_profile', '&&'] + raw_command

        # Get p4 environment variables from plugin preferences.
        settings = load_settings()
        environ = os.environ
        for name in PERFORCE_ENVIRONMENT_VARIABLES:
            value = settings.get(name)
            if value:
                environ[name] = value

        # And put them into the environment.
        if 'env' in kwargs:
            kwargs['env'].update(environ)
        else:
            kwargs['env'] = environ

        if self.verbose:
            display_message(message)

        callback = callback or self.generic_done
        thread = CommandThread(raw_command,
            functools.partial(self.check_output, callback), **kwargs)
        thread.start()
        ThreadProgress(thread, message)

    def check_output(self, callback, output, retcode):
        p4_failed = output.startswith(PERFORCE_P4_CLIENT_ERROR_MESSAGE)

        if not p4_failed:
            cleaned = []
            for line in output.splitlines()[:-1]:  # skip line 'exit: <number>'
                if any(line.startswith(prefix) for prefix in PERFORCE_P4_OUTPUT_PREFIXES):
                    prefix, _, message = line.partition(': ')
                    p4_failed = (prefix == PERFORCE_P4_ERROR_PREFIX and
                        message not in self.allowed_errors)
                    cleaned.append(message)
                else:
                    cleaned.append(line)
            output = '\n'.join(cleaned)

        if p4_failed or retcode != 0:
            self._print_p4_output(output)
            main_thread(sublime.status_message,
                'Something went wrong, see console for details')
        else:
            if self.verbose:
                self._print_p4_output(output)
            callback(output)

    def _print_p4_output(self, output):

        def wrap(message, length=80):
            return (' %s ' % message).center(length, '-')

        print '\n' + '\n'.join((
            self.command,
            wrap(PERFORCE_P4_OUTPUT_START_MESSAGE),
            output,
            wrap(PERFORCE_P4_OUTPUT_END_MESSAGE),
        ))

    def generic_done(self, output):
        # TODO: implement
        pass

    def _output_to_view(self, output_file, output, clear=False,
            syntax='Packages/Diff/Diff.tmLanguage'):
        output_file.set_syntax_file(syntax)
        edit = output_file.begin_edit()
        if clear:
            region = sublime.Region(0, self.output_view.size())
            output_file.erase(edit, region)
        output_file.insert(edit, 0, output)
        output_file.end_edit(edit)

    def scratch(self, output, title='', **kwargs):
        scratch_file = self.active_window().new_file()
        if title:
            scratch_file.set_name(title)
        scratch_file.set_scratch(True)
        self._output_to_view(scratch_file, output, **kwargs)
        scratch_file.set_read_only(True)
        return scratch_file

    def panel(self, output, **kwargs):
        if not hasattr(self, 'output_view'):
            self.output_view = self.active_window().get_output_panel('perforce')
        self.output_view.set_read_only(False)
        self._output_to_view(self.output_view, output, clear=True, **kwargs)
        self.output_view.set_read_only(True)
        self.active_window().run_command('show_panel',
            {'panel': 'output.perforce'})

    def quick_panel(self, *args, **kwargs):
        self.active_window().show_quick_panel(*args, **kwargs)

    def input_panel(self, caption, initial, **kwargs):
        for callback in ('on_done', 'on_change', 'on_cancel'):
            kwargs.setdefault(callback, None)
        self.active_window().show_input_panel(caption, initial, **kwargs)


class PerforceGenericCommand(PerforceCommand):
    def p4info(self, callback):

        def parse(output):
            result = {}
            for line in output.splitlines():
                key, _, value = line.partition(': ')
                result[key.replace(' ', '_').lower()] = value
            callback(result)

        self.run_command(['info'], callback=parse)

    def get_current_user(self, callback):
        self.p4info(callback=lambda info: callback(info['user_name']))

    def get_pending_changelists(self, callback):

        def get_raw_changes(username):

            def parse(output):
                result = []
                for match in PERFORCE_P4_CHANGES_CL_RE.finditer(output):
                    data = match.groupdict()

                    # Perforce adds leading and/or trailing space characters
                    # to the changelist description.
                    # I could not find any pattern, so I just remove all
                    # leading and trailing spaces.
                    data['description'] = data['description'].strip()

                    result.append(data)

                callback(result)

            self.run_command(['changes', '-s', 'pending', '-u', username],
                 callback=parse)

        self.get_current_user(callback=get_raw_changes)

    def get_client_root(self, callback):

        def get_value(info_dict):
            client_root = info_dict.get('client_root', None)
            if client_root is None:
                # TODO: show in panel or edit client in ST2
                main_thread(sublime.error_message,
                    "Perforce: Please configure clientspec. Launching 'p4 client'...")
                self.run_command(['client'])
            else:
                callback(os.path.normpath(client_root))

        self.p4info(callback=get_value)

    def is_under_client_root(self, candidate, callback):

        def check(root):
            # Function os.path.commonprefix doesn't parse paths,
            # need to normalize paths case and separators before call.
            normalize = lambda path: os.path.normcase(os.path.normpath(path))
            prefix = os.path.commonprefix(map(normalize, (candidate, root)))
            # Due to lack of the os.path.samefile on Python 2.x for Windows
            # we should compare paths directly.
            callback(root == prefix)

        self.get_client_root(callback=check)

    def check_depot_file(self, callback):  # TODO: rename method
        def root_check_done(filename, is_in_depot):
            if is_in_depot:
                callback(filename)
            else:
                display_message('File is not under the client root')

        filename = self.active_view().file_name()
        if filename:
            self.is_under_client_root(filename,
                callback=functools.partial(root_check_done, filename))
        else:
            display_message('View does not contain a file')


class PerforceWindowCommand(PerforceGenericCommand, sublime_plugin.WindowCommand):
    def active_view(self):
        return self.window.active_view()

    def active_window(self):
        return self.window


class PerforceTextCommand(PerforceGenericCommand, sublime_plugin.TextCommand):
    def active_view(self):
        return self.view

    def active_window(self):
        return self.view.window() or sublime.active_window()


class PerforceAddCommand(PerforceTextCommand):
    def run(self, edit):
        self.check_depot_file(callback=self.check_passed)

    def check_passed(self, filename):
        self.run_command(['add', filename], verbose=True)


class PerforceDiffCommand(PerforceTextCommand):
    def run(self, edit):
        self.check_depot_file(callback=self.check_passed)

    def check_passed(self, filename):
        self.run_command(['diff', filename], callback=self.diff_done)

    def diff_done(self, result):
        # TODO: show diff in output panel
        if PERFORCE_P4_DIFF_HEADER_RE.match(result):
            self.panel('No output')
        else:
            # TODO: mark new/removed lines
            self.scratch(result, title='Perforce Diff')


class PerforceCreateChangelistCommand(PerforceWindowCommand):
    def run(self):
        self.input_panel('Changelist Description', '', self.description_entered)

    def description_entered(self, description):
        # Get default changelist specification.
        self.run_command(['change', '-o'],
            callback=functools.partial(self.specification_obtained, description))

    def specification_obtained(self, user_description, result):
        # According to Perforce Knowledge Base,
        # all lines in the description must start with a space or tab.
        # See http://kb.perforce.com/article/6 for details.
        buffer_ = []
        for line in user_description.splitlines():
            if not line.startswith(' '):
                line = ' ' + line
            buffer_.append(line)
        # TODO: use linesep from settings?
        user_description = '\n'.join(buffer_)

        # Replace the default description on entered by user
        # and remove all files from the new changelist.
        result = (result[:result.find(PERFORCE_DEFAULT_DESCRIPTION)] +
            user_description)

        # Create changelist.
        self.run_command(['change', '-i'], stdin=result,
            callback=self.on_created)

    def on_created(self, result):
        self.panel(result)


class PerforceDeleteCommand(PerforceTextCommand):
    def run(self, edit):
        self.check_depot_file(callback=self.check_passed)

    def check_passed(self, filename):
        self.run_command(['delete', filename],
            callback=functools.partial(self.delete_done, filename))

    def delete_done(self, filename, result):
        if os.path.exists(filename):
            # Can't delete file for some reason.
            self.panel(result)
        else:
            # File was deleted, close view.
            self.active_window().run_command('close')


class PerforceCheckoutCommand(PerforceTextCommand):
    def run(self, edit):
        self.check_depot_file(callback=self.check_passed)

    def check_passed(self, filename):
        if is_writable(filename):
            display_message('File is already writable')
        else:
            self.run_command(['edit', filename],
                callback=functools.partial(self.checkout_done, filename))

    def checkout_done(self, filename, result):
        if not is_writable(filename):
            # Can't checkout file for some reason.
            self.panel(result)


class PerforceSubmitCommand(PerforceWindowCommand):
    def run(self):
        self.get_pending_changelists(callback=self.changelists_recieved)

    def changelists_recieved(self, changelists):
        if changelists:
            self.changelists = changelists
            format = '%(number)s - %(description)s'
            self.quick_panel([(format % cl) for cl in changelists],
                self.on_pick)
        else:
            self.panel('There are no pending changelists')

    def on_pick(self, picked):
        if picked != -1:
            number = self.changelists[picked]['number']
            self.run_command(['submit', '-c', number],
                callback=self.submit_done)

    def submit_done(self, result):
        # TODO: handle 'No files to submit' in check_output()
        self.panel(result)


class PerforceListCheckedOutFilesCommand(PerforceWindowCommand):
    def run(self):
        self.get_pending_changelists(callback=self.changelists_recieved)

    def changelists_recieved(self, changelists):
        default_changelist = {
            'number': 'default',
            'description': '<no description>'
        }
        changelists.append(default_changelist)
        self.changelists = changelists
        self.files = []
        self.extract_next()

    def extract_next(self):
        current = self.changelists.pop()
        self.run_command(['opened', '-c', current['number']],
            callback=functools.partial(self.process_extracted, current),
            allowed_errors=[PERFORCE_P4_NO_OPENED_FILES_ERROR])

    def process_extracted(self, changelist, output):
        print output.splitlines()
        if self.changelists:
            self.extract_next()
        else:
            self.extracting_done()

    def extracting_done(self):
        if self.files:
            self.quick_panel(self.files, self.on_pick)
        else:
            self.panel('There are no checked out files')

    def on_pick(self, picked):
        if picked != -1:
            pass
            #self.active_window().open_file()


class ListCheckedOutFilesThread(threading.Thread):
    def __init__(self, window):
        self.window = window
        threading.Thread.__init__(self)

    def ConvertFileNameToFileOnDisk(self, in_filename):
        clientroot = GetClientRoot(os.path.dirname(in_filename))
        if(clientroot == -1):
            return 0

        filename = clientroot + os.sep + in_filename.replace('\\', os.sep).replace('/', os.sep)

        return filename

    def MakeFileListFromChangelist(self, in_changelistline):
        files_list = []

        # Launch p4 opened to retrieve all files from changelist
        command = ConstructCommand('p4 opened -c ' + in_changelistline[1])
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
        result, err = p.communicate()
        if(not err):
            lines = result.splitlines()
            for line in lines:
                # remove the change #
                poundindex = line.rfind('#')
                cleanedfile = line[0:poundindex]

                # just keep the filename
                cleanedfile = '/'.join(cleanedfile.split('/')[3:])

                file_entry = [cleanedfile[cleanedfile.rfind('/')+1:]]
                file_entry.append("Changelist: " + in_changelistline[1])
                file_entry.append(' '.join(in_changelistline[7:]));
                localfile = self.ConvertFileNameToFileOnDisk(cleanedfile)
                if(localfile != 0):
                    file_entry.append(localfile)
                    files_list.append(file_entry)
        return files_list

    def MakeCheckedOutFileList(self):
        files_list = self.MakeFileListFromChangelist(['','default','','','','','','Default Changelist']);

        currentuser = GetUserFromClientspec()
        if(currentuser == -1):
            return files_list

        # Launch p4 changes to retrieve all the pending changelists
        command = ConstructCommand('p4 changes -s pending -u ' + currentuser);
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
        result, err = p.communicate()

        if(not err):
            changelists = result.splitlines()

            # for each line, extract the change, and run p4 opened on it to list all the files
            for changelistline in changelists:
                changelistlinesplit = changelistline.split(' ')
                files_list.extend(self.MakeFileListFromChangelist(changelistlinesplit))

        return files_list

    def on_done(self, picked):
        if picked == -1:
            return
        file_name = self.files_list[picked][3]

        def open_file():
            self.window.open_file(file_name)
        sublime.set_timeout(open_file, 10)


def IsFileInDepot(in_folder, in_filename):
    isUnderClientRoot = IsFolderUnderClientRoot(in_folder);
    if(os.path.isfile(os.path.join(in_folder, in_filename))): # file exists on disk, not being added
        if(isUnderClientRoot):
            return 1
        else:
            return 0
    else:
        if(isUnderClientRoot):
            return -1 # will be in the depot, it's being added
        else:
            return 0


def AppendToChangelistDescription(changelist, input):
    # First, create an empty changelist, we will then get the cl number and set the description
    command = ConstructCommand('p4 change -o ' + changelist)
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
    result, err = p.communicate()

    if(err):
        return 0, err

    # Find the description field and modify it
    lines = result.splitlines()

    descriptionindex = -1
    for index, line in enumerate(lines):
        if(line.strip() == "Description:"):
            descriptionindex = index
            break;

    filesindex = -1
    for index, line in enumerate(lines):
        if(line.strip() == "Files:"):
            filesindex = index
            break;

    if(filesindex == -1): # The changelist is empty
        endindex = index
    else:
        endindex = filesindex - 1

    perforce_settings = sublime.load_settings('Perforce.sublime-settings')
    lines.insert(endindex , "\t" + input)

    temp_changelist_description_file = open(os.path.join(tempfile.gettempdir(), "tempchangelist.txt"), 'w')

    try:
        temp_changelist_description_file.write(perforce_settings.get('perforce_end_line_separator').join(lines))
    finally:
        temp_changelist_description_file.close()

    command = ConstructCommand('p4 change -i < ' + temp_changelist_description_file.name)
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
    result, err = p.communicate()

    # Clean up
    os.unlink(temp_changelist_description_file.name)

    if(err):
        return 0, err

    return 1, result


def WarnUser(message):
    perforce_settings = sublime.load_settings('Perforce.sublime-settings')
    if(perforce_settings.get('perforce_warnings_enabled')):
        if(perforce_settings.get('perforce_log_warnings_to_status')):
            sublime.status_message("Perforce [warning]: " + message)
        else:
            print "Perforce [warning]: " + message

def LogResults(success, message):
    if(success >= 0):
        print "Perforce: " + message
    else:
        WarnUser(message);

class PerforceAutoCheckout(sublime_plugin.EventListener):
    def on_modified(self, view):
        return
        if(not view.file_name()):
            return

        if(IsFileWritable(view.file_name())):
            return

        perforce_settings = sublime.load_settings('Perforce.sublime-settings')

        # check if this part of the plugin is enabled
        if(not perforce_settings.get('perforce_auto_checkout') or not perforce_settings.get('perforce_auto_checkout_on_modified')):
            return

        if(view.is_dirty()):
            success, message = Checkout(view.file_name())
            LogResults(success, message);

    def on_pre_save(self, view):
        return
        perforce_settings = sublime.load_settings('Perforce.sublime-settings')

        # check if this part of the plugin is enabled
        if(not perforce_settings.get('perforce_auto_checkout') or not perforce_settings.get('perforce_auto_checkout_on_save')):
            return

        if(view.is_dirty()):
            success, message = Checkout(view.file_name())
            LogResults(success, message);


class PerforceAutoAdd(sublime_plugin.EventListener):
    preSaveIsFileInDepot = 0
    def on_pre_save(self, view):
        # file already exists, no need to add
        if view.file_name() and os.path.isfile(view.file_name()):
            return

        global global_folder
        global_folder, filename = os.path.split(view.file_name())

        perforce_settings = sublime.load_settings('Perforce.sublime-settings')

        self.preSaveIsFileInDepot = 0

        # check if this part of the plugin is enabled
        if(not perforce_settings.get('perforce_auto_add')):
            WarnUser("Auto Add disabled")
            return

        folder_name, filename = os.path.split(view.file_name())
        self.preSaveIsFileInDepot = IsFileInDepot(folder_name, filename)

    def on_post_save(self, view):
        if(self.preSaveIsFileInDepot == -1):
            folder_name, filename = os.path.split(view.file_name())
            success, message = Add(folder_name, filename)
            LogResults(success, message)

# Rename section
def Rename(in_filename, in_newname):
    command = ConstructCommand('p4 integrate -d -t -Di -f "' + in_filename + '" "' + in_newname + '"')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
    result, err = p.communicate()

    if(err):
        return 0, err.strip()

    command = ConstructCommand('p4 delete "' + in_filename + '" "' + in_newname + '"')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
    result, err = p.communicate()

    if(not err):
        return 1, result.strip()
    else:
        return 0, err.strip()

class PerforceRenameCommand(sublime_plugin.WindowCommand):
    def run(self):
        # Get the description
        self.window.show_input_panel('New File Name', self.window.active_view().file_name(),
            self.on_done, self.on_change, self.on_cancel)

    def on_done(self, input):
        success, message = Rename(self.window.active_view().file_name(), input)
        if(success):
            self.window.run_command('close')
            self.window.open_file(input)

        LogResults(success, message)

    def on_change(self, input):
        pass

    def on_cancel(self):
        pass

# Revert section
def Revert(in_folder, in_filename):
    # revert the file
    return PerforceCommandOnFile("revert", in_folder, in_filename);

class PerforceRevertCommand(sublime_plugin.TextCommand):
    def run_(self, args): # revert cannot be called when an Edit object exists, manually handle the run routine
        if(self.view.file_name()):
            folder_name, filename = os.path.split(self.view.file_name())

            if(IsFileInDepot(folder_name, filename)):
                success, message = Revert(folder_name, filename)
                if(success): # the file was properly reverted, ask Sublime Text to refresh the view
                    self.view.run_command('revert');
            else:
                success = 0
                message = "File is not under the client root."

            LogResults(success, message)
        else:
            WarnUser("View does not contain a file")

# Graphical Diff With Depot section
class GraphicalDiffThread(threading.Thread):
    def __init__(self, in_folder, in_filename, in_endlineseparator, in_command):
        self.folder = in_folder
        self.filename = in_filename
        self.endlineseparator = in_endlineseparator
        self.command = in_command
        threading.Thread.__init__(self)

    def run(self):
        success, content = PerforceCommandOnFile("print", self.folder, self.filename)
        if(not success):
            return 0, content

        # Create a temporary file to hold the depot version
        depotFileName = "depot"+self.filename
        tmp_file = open(os.path.join(tempfile.gettempdir(), depotFileName), 'w')

        # Remove the first two lines of content
        linebyline = content.splitlines();
        content=self.endlineseparator.join(linebyline[1:]);

        try:
            tmp_file.write(content)
        finally:
            tmp_file.close()

        # Launch P4Diff with both files and the same arguments P4Win passes it
        diffCommand = self.command
        diffCommand = diffCommand.replace('%depotfile_path', tmp_file.name)
        diffCommand = diffCommand.replace('%depotfile_name', depotFileName)
        diffCommand = diffCommand.replace('%file_path', os.path.join(self.folder, self.filename))
        diffCommand = diffCommand.replace('%file_name', self.filename)

        command = ConstructCommand(diffCommand)

        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
        result, err = p.communicate()

        # Clean up
        os.unlink(tmp_file.name);

def GraphicalDiffWithDepot(self, in_folder, in_filename):
    perforce_settings = sublime.load_settings('Perforce.sublime-settings')
    diffcommand = perforce_settings.get('perforce_selectedgraphicaldiffapp_command')
    if not diffcommand:
        diffcommand = perforce_settings.get('perforce_default_graphical_diff_command')
    GraphicalDiffThread(in_folder, in_filename, perforce_settings.get('perforce_end_line_separator'), diffcommand).start()

    return 1, "Launching thread for Graphical Diff"

class PerforceGraphicalDiffWithDepotCommand(sublime_plugin.TextCommand):
    def run(self, edit):
        if(self.view.file_name()):
            folder_name, filename = os.path.split(self.view.file_name())

            if(IsFileInDepot(folder_name, filename)):
                success, message = GraphicalDiffWithDepot(self, folder_name, filename)
            else:
                success = 0
                message = "File is not under the client root."

            LogResults(success, message)
        else:
            WarnUser("View does not contain a file")

class PerforceSelectGraphicalDiffApplicationCommand(sublime_plugin.WindowCommand):
    def run(self):
        diffapps = []
        if os.path.exists(perforceplugin_dir + os.sep + 'graphicaldiffapplications.json'):
            f = open(perforceplugin_dir + os.sep + 'graphicaldiffapplications.json')
            applications = json.load(f)
            f.close()

            for entry in applications.get('applications'):
                formattedentry = []
                formattedentry.append(entry.get('name'))
                formattedentry.append(entry.get('exename'))
                diffapps.append(formattedentry)

        self.window.show_quick_panel(diffapps, self.on_done)
    def on_done(self, picked):
        if picked == -1:
            return

        f = open(perforceplugin_dir + os.sep + 'graphicaldiffapplications.json')
        applications = json.load(f)
        entry = applications.get('applications')[picked]
        f.close()

        sublime.status_message(__name__ + ': Please make sure that ' + entry['exename'] + " is reachable - you might need to restart Sublime Text 2.")

        settings = sublime.load_settings('Perforce.sublime-settings')
        settings.set('perforce_selectedgraphicaldiffapp', entry['name'])
        settings.set('perforce_selectedgraphicaldiffapp_command', entry['diffcommand'])
        sublime.save_settings('Perforce.sublime-settings')

# Move Current File to Changelist
def MoveFileToChangelist(in_filename, in_changelist):
    folder_name, filename = os.path.split(in_filename)

    command = ConstructCommand('p4 reopen -c ' + in_changelist + ' "' + filename + '"')
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
    result, err = p.communicate()

    if(err):
        return 0, err

    return 1, result

class ListChangelistsAndMoveFileThread(threading.Thread):
    def __init__(self, window):
        self.window = window
        self.view = window.active_view()
        threading.Thread.__init__(self)

    def MakeChangelistsList(self):
        success, rawchangelists = GetPendingChangelists();

        resultchangelists = ['New', 'Default'];

        if(success):
            changelists = rawchangelists.splitlines()

            # for each line, extract the change
            for changelistline in changelists:
                changelistlinesplit = changelistline.split(' ')

                # Insert at two because we receive the changelist in the opposite order and want to keep new and default on top
                resultchangelists.insert(2, "Changelist " + changelistlinesplit[1] + " - " + ' '.join(changelistlinesplit[7:]))

        return resultchangelists

    def run(self):
        self.changelists_list = self.MakeChangelistsList()

        def show_quick_panel():
            if not self.changelists_list:
                sublime.error_message(__name__ + ': There are no changelists to list.')
                return
            self.window.show_quick_panel(self.changelists_list, self.on_done)

        sublime.set_timeout(show_quick_panel, 10)

    def on_done(self, picked):
        if picked == -1:
            return
        changelistlist = self.changelists_list[picked].split(' ')

        def move_file():
            changelist = 'Default'
            if(len(changelistlist) > 1): # Numbered changelist
                changelist = changelistlist[1]
            else:
                changelist = changelistlist[0]

            if(changelist == 'New'): # Special Case
                self.window.show_input_panel('Changelist Description', '', self.on_description_done, self.on_description_change, self.on_description_cancel)
            else:
                success, message = MoveFileToChangelist(self.view.file_name(), changelist.lower())
                LogResults(success, message);

        sublime.set_timeout(move_file, 10)

    def on_description_done(self, input):
        success, message = CreateChangelist(input)
        if(success == 1):
            # Extract the changelist name from the message
            changelist = message.split(' ')[1]
            # Move the file
            success, message = MoveFileToChangelist(self.view.file_name(), changelist)

        LogResults(success, message)

    def on_description_change(self, input):
        pass

    def on_description_cancel(self):
        pass

class PerforceMoveCurrentFileToChangelistCommand(sublime_plugin.WindowCommand):
    def run(self):
        # first, test if the file is under the client root
        folder_name, filename = os.path.split(self.window.active_view().file_name())
        isInDepot = IsFileInDepot(folder_name, filename)

        if(isInDepot != 1):
            WarnUser("File is not under the client root.")
            return 0

        ListChangelistsAndMoveFileThread(self.window).start()

# Add Line to Changelist Description
class AddLineToChangelistDescriptionThread(threading.Thread):
    def __init__(self, window):
        self.window = window
        self.view = window.active_view()
        threading.Thread.__init__(self)

    def MakeChangelistsList(self):
        success, rawchangelists = GetPendingChangelists();

        resultchangelists = [];

        if(success):
            changelists = rawchangelists.splitlines()

            # for each line, extract the change, and run p4 opened on it to list all the files
            for changelistline in changelists:
                changelistlinesplit = changelistline.split(' ')

                # Insert at zero because we receive the changelist in the opposite order
                # Might be more efficient to sort...
                changelist_entry = ["Changelist " + changelistlinesplit[1]]
                changelist_entry.append(' '.join(changelistlinesplit[7:]));

                resultchangelists.insert(0, changelist_entry)

        return resultchangelists

    def run(self):
        self.changelists_list = self.MakeChangelistsList()

        def show_quick_panel():
            if not self.changelists_list:
                sublime.error_message(__name__ + ': There are no changelists to list.')
                return
            self.window.show_quick_panel(self.changelists_list, self.on_done)

        sublime.set_timeout(show_quick_panel, 10)

    def on_done(self, picked):
        if picked == -1:
            return
        changelistlist = self.changelists_list[picked][0].split(' ')

        def get_description_line():
            self.changelist = changelistlist[1]
            self.window.show_input_panel('Changelist Description', '', self.on_description_done, self.on_description_change, self.on_description_cancel)

        sublime.set_timeout(get_description_line, 10)

    def on_description_done(self, input):
        success, message = AppendToChangelistDescription(self.changelist, input)

        LogResults(success, message)

    def on_description_change(self, input):
        pass

    def on_description_cancel(self):
        pass

class PerforceAddLineToChangelistDescriptionCommand(sublime_plugin.WindowCommand):
    def run(self):
        AddLineToChangelistDescriptionThread(self.window).start()

class PerforceLogoutCommand(sublime_plugin.WindowCommand):
    def run(self):
        try:
            command = ConstructCommand("p4 set P4PASSWD=")
            p = subprocess.Popen(command, stdin=subprocess.PIPE,stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
            p.communicate()
        except ValueError:
            pass

class PerforceLoginCommand(sublime_plugin.WindowCommand):
    def run(self):
        self.window.show_input_panel("Enter Perforce Password", "", self.on_done, None, None)

    def on_done(self, password):
        try:
            command = ConstructCommand("p4 logout")
            p = subprocess.Popen(command, stdin=subprocess.PIPE,stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
            p.communicate()
            #unset var
            command = ConstructCommand("p4 set P4PASSWD=" + password)
            p = subprocess.Popen(command, stdin=subprocess.PIPE,stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
            p.communicate()
        except ValueError:
            pass

class PerforceUnshelveClCommand(sublime_plugin.WindowCommand):
    def run(self):
        try:
            ShelveClCommand(self.window, False).start()
        except:
            WarnUser("Unknown Error, does the included P4 Version support Shelve?")
            return -1
class PerforceShelveClCommand(sublime_plugin.WindowCommand):
    def run(self):
        try:
            ShelveClCommand(self.window, True).start()
        except:
            WarnUser("Unknown Error, does the included P4 Version support Shelve?")
            return -1

class ShelveClCommand(threading.Thread):
    def __init__(self, window, shelve=True):
        self.shelve = shelve
        self.window = window
        threading.Thread.__init__(self)

    def run(self):
        self.changelists_list = self.MakeChangelistsList()
        def show_quick_panel():
            if not self.changelists_list:
                sublime.error_message(__name__ + ': There are no changelists to list.')
                return
            self.window.show_quick_panel(self.changelists_list, self.on_done)

        sublime.set_timeout(show_quick_panel, 10)

    def on_done(self, picked):
        if picked == -1:
            return
        changelistlist = self.changelists_list[picked].split(' ')


        changelist = 'Default'
        if(len(changelistlist) > 1): # Numbered changelist
            changelist = changelistlist[1]
        else:
            changelist = changelistlist[0]

        print changelist


        if self.shelve:
            cmdString = "shelve -c" + changelist
        else:
            cmdString = "unshelve -s" + changelist + " -f"
        command = ConstructCommand("p4 " + cmdString)
        p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=global_folder, shell=True)
        result, err = p.communicate()
        print result
        if(err):
            WarnUser("usererr " + err.strip())
            return -1

    def MakeChangelistsList(self):
        success, rawchangelists = GetPendingChangelists();

        resultchangelists = []

        if(success):
            changelists = rawchangelists.splitlines()

            # for each line, extract the change
            for changelistline in changelists:
                changelistlinesplit = changelistline.split(' ')

                resultchangelists.insert(0, "Changelist " + changelistlinesplit[1] + " - " + ' '.join(changelistlinesplit[7:]))

        return resultchangelists


class PerforceTestCase(unittest.TestCase):
    def test_test(self):
        def on_done(result):
            print (result,)

        is_under_client_root('/home/roman/aslkdj', on_done)


class PerforceRunUnitTests(sublime_plugin.WindowCommand):
    def run(self):
        tests = [
            'test_test',
        ]
        suite = unittest.TestSuite(map(PerforceTestCase, tests))
        unittest.TextTestRunner(verbosity=2).run(suite)
