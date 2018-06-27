"""
retirejs.py

Copyright 2018 Andres Riancho

This file is part of w3af, http://w3af.org/ .

w3af is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation version 2 of the License.

w3af is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with w3af; if not, write to the Free Software
Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA

"""
import os
import json
import hashlib
import tempfile
import subprocess

from threading import Timer

import w3af.core.controllers.output_manager as om
import w3af.core.data.constants.severity as severity

from w3af.core.controllers.plugins.grep_plugin import GrepPlugin
from w3af.core.controllers.misc.which import which
from w3af.core.data.db.disk_set import DiskSet
from w3af.core.data.kb.vuln import Vuln


class retirejs(GrepPlugin):
    """
    Uses retirejs to identify javascript libraries with known vulnerabilities

    :author: Andres Riancho (andres.riancho@gmail.com)
    """

    METHODS = ('GET',)
    HTTP_CODES = (200,)
    RETIRE_CMD = 'retire -j --outputformat json --outputpath %s --jspath %s'
    RETIRE_TIMEOUT = 5

    def __init__(self):
        GrepPlugin.__init__(self)

        self._analyzed_hashes = DiskSet(table_prefix='retirejs')
        self._retirejs_path = self._get_retirejs_path()
        self._retirejs_exit_code_result = None
        self._retirejs_exit_code_was_run = False

    def grep(self, request, response):
        """
        Send HTTP responses to retirejs and parse JSON output.

        For performance, avoid running retirejs on the same file more than once.

        :param request: The HTTP request object.
        :param response: The HTTP response object
        :return: None
        """
        if not self._retirejs_exit_code():
            return

        if request.get_method() not in self.METHODS:
            return

        if response.get_code() not in self.HTTP_CODES:
            return

        if not response.is_text_or_html():
            return

        if not self._should_analyze(response):
            return

        self._analyze_response(response)

    def end(self):
        self._analyzed_hashes.cleanup()

    def _retirejs_exit_code(self):
        """
        Runs retirejs on an empty file to check that the return code is 0, this
        is just a safety check to make sure everything is working. It is only
        run once.

        :return: True if everything works
        """
        if self._retirejs_exit_code_was_run:
            return self._retirejs_exit_code_result

        check_file = tempfile.NamedTemporaryFile(prefix='retirejs-check-',
                                                 suffix='.w3af',
                                                 delete=False)
        check_file.write('')
        check_file.close()

        output_file = tempfile.NamedTemporaryFile(prefix='retirejs-output-',
                                                  suffix='.w3af',
                                                  delete=False)
        output_file.close()

        args = (output_file.name, check_file.name)
        cmd = self.RETIRE_CMD % args

        try:
            subprocess.check_output(cmd, shell=True)
        except subprocess.CalledProcessError:
            msg = ('Unexpected retire.js exit code.'
                   ' Disabling grep.retirejs plugin.')
            om.out.error(msg)

            self._retirejs_exit_code_was_run = True
            self._retirejs_exit_code_result = False
        else:
            om.out.debug('retire.js returned the expected exit code.')

            self._retirejs_exit_code_was_run = True
            self._retirejs_exit_code_result = True
        finally:
            self._remove_file(output_file.name)
            self._remove_file(check_file.name)

        return self._retirejs_exit_code_result

    def _should_analyze(self, response):
        """
        :param response: HTTP response
        :return: True if we should analyze this HTTP response
        """
        #
        # Avoid running this plugin twice on the same URL
        #
        url_hash = hashlib.md5(response.get_url().url_string).hexdigest()
        if url_hash in self._analyzed_hashes:
            return False

        self._analyzed_hashes.add(url_hash)

        #
        # Avoid running this plugin twice on the same file content
        #
        response_hash = hashlib.md5(response.get_body()).hexdigest()

        if response_hash in self._analyzed_hashes:
            return False

        self._analyzed_hashes.add(response_hash)
        return True

    def _analyze_response(self, response):
        """
        :return: None, save the findings to the KB.
        """
        response_file = self._save_response_to_file(response)
        json_doc = self._analyze_file(response_file)
        self._remove_file(response_file)
        self._json_to_kb(response, json_doc)

    def _save_response_to_file(self, response):
        # Note: The file needs to have .js extension to force retirejs to
        #       scan it. Any other extension will be ignored.
        response_file = tempfile.NamedTemporaryFile(prefix='retirejs-response-',
                                                    suffix='.w3af.js',
                                                    delete=False)

        response_file.write(response.get_body())
        response_file.close()

        return response_file.name

    def _analyze_file(self, response_file):
        """
        Analyze a file and return the result as JSON

        :param response_file: File holding HTTP response body
        :return: JSON document
        """
        json_file = tempfile.NamedTemporaryFile(prefix='retirejs-output-',
                                                suffix='.w3af',
                                                delete=False)
        json_file.close()

        args = (json_file.name, response_file)
        cmd = self.RETIRE_CMD % args

        process = subprocess.Popen(cmd, shell=True)

        # This will terminate the retirejs process in case it hangs
        t = Timer(self.RETIRE_TIMEOUT, kill, [process])
        t.start()

        # Wait for the retirejs process to complete
        process.wait()

        # Cancel the timer if it wasn't run
        t.cancel()

        # retirejs will return code != 0 when a vulnerability is found
        # we use this to decide when we need to parse the output
        json_doc = []

        if process.returncode != 0:
            try:
                json_doc = json.loads(file(json_file.name).read())
            except Exception, e:
                msg = 'Failed to parse retirejs output. Exception: "%s"'
                om.out.debug(msg % e)

        self._remove_file(json_file.name)
        return json_doc

    def _remove_file(self, response_file):
        """
        Remove a file from disk. Don't fail if the file doesn't exist
        :param response_file: The file path to remove
        :return: None
        """
        try:
            os.remove(response_file)
        except:
            pass

    def _json_to_kb(self, response, json_doc):
        """
        Write the findings which are in JSON retirejs format to the KB.

        :param response: HTTP response
        :param json_doc: The whole JSON document as returned by retirejs
        :return: None, everything is written to the KB.
        """
        for json_finding in json_doc:
            self._handle_finding(response, json_finding)

    def _handle_finding(self, response, json_finding):
        """
        Write a finding to the KB.

        :param response: HTTP response
        :param json_finding: A finding from retirejs JSON document
        :return: None, everything is written to the KB.
        """
        results = json_finding.get('results', [])
        for json_result in results:
            self._handle_result(response, json_result)

    def _handle_result(self, response, json_result):
        """
        Write a result to the KB.

        :param response: HTTP response
        :param json_result: A finding from retirejs JSON document
        :return: None, everything is written to the KB.
        """
        version = json_result.get('version', None)
        component = json_result.get('component', None)
        vulnerabilities = json_result.get('vulnerabilities', [])

        if version is None or component is None:
            om.out.debug('The retirejs generated JSON document is invalid.'
                         ' Either the version or the component attribute is'
                         ' missing. Will ignore this result and continue with'
                         ' the next.')
            return

        if not vulnerabilities:
            om.out.debug('The retirejs generated JSON document is invalid. No'
                         ' vulnerabilities were found. Will ignore this result'
                         ' and continue with the next.')
            return

        message = VulnerabilityMessage(response, component, version)

        for vulnerability in vulnerabilities:
            vuln_severity = vulnerability.get('severity', 'unknown')
            summary = vulnerability.get('identifiers', {}).get('summary', 'unknown')
            info_urls = vulnerability.get('info', [])

            retire_vuln = RetireJSVulnerability(vuln_severity, summary, info_urls)
            message.add_vulnerability(retire_vuln)

        desc = message.to_string()
        real_severity = message.get_severity()

        v = Vuln('Vulnerable JavaScript library in use',
                 desc,
                 real_severity,
                 response.get_id(),
                 self.get_name())

        v.set_uri(response.get_uri())

        self.kb_append_uniq(self, 'js', v, filter_by='URL')

    def _get_retirejs_path(self):
        """
        :return: Path to the retirejs binary
        """
        paths_to_retire = which('retire')

        # The dependency check script guarantees that there will always be
        # at least one installation of the retirejs command.
        return paths_to_retire[0]

    def get_long_desc(self):
        """
        :return: A DETAILED description of the plugin functions and features.
        """
        return """
        Uses retirejs [0] to identify vulnerable javascript libraries in HTTP
        responses.
        
        [0] https://github.com/retirejs/retire.js/
        """


class VulnerabilityMessage(object):
    def __init__(self, response, component, version):
        self.response = response
        self.component = component
        self.version = version
        self.vulnerabilities = []

    def add_vulnerability(self, vulnerability):
        self.vulnerabilities.append(vulnerability)

    def get_severity(self):
        """
        The severity which is shown by retirejs is, IMHO, too high. For example
        a reDoS vulnerability in a JavaScript library is sometimes tagged as
        high.

        We reduce the vulnerability associated with the vulnerabilities a
        little bit here, to match what we find in other plugins.

        :return: severity.MEDIUM if there is at least one high in
                 self.vulnerabilities, otherwise just return severity.LOW
        """
        for vulnerability in self.vulnerabilities:
            if vulnerability.severity.lower() == 'high':
                return severity.MEDIUM

        return severity.LOW

    def to_string(self):
        message = ('A JavaScript library with known vulnerabilities was'
                   ' identified at %(url)s. The library was identified as'
                   ' "%(component)s" version %(version)s and has these known'
                   ' vulnerabilities:\n'
                   '\n'
                   '%(summaries)s\n'
                   '\n'
                   'Consider updating to the latest stable release of the'
                   ' affected library.')

        summaries = '\n'.join(' - %s' % vuln.summary for vuln in self.vulnerabilities)

        args = {'url': self.response.get_url(),
                'component': self.component,
                'version': self.version,
                'summaries': summaries}

        return message % args


class RetireJSVulnerability(object):
    def __init__(self, vuln_severity, summary, info_urls):
        self.severity = vuln_severity
        self.summary = summary
        self.info_urls = info_urls


def kill(process):
    try:
        process.terminate()
    except OSError:
        # ignore
        pass
