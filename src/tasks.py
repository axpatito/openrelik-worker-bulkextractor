# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import subprocess

from openrelik_worker_common.file_utils import create_output_file
from openrelik_worker_common.task_utils import create_task_result, get_input_files
from openrelik_worker_common.reporting import Report, MarkdownTable, serialize_file_report
from uuid import uuid4
import os
import xml.etree.ElementTree as xml_tree
import glob
from typing import List, Dict
import shutil

from .app import celery

# Task name used to register and route the task to the correct queue.
TASK_NAME = "openrelik-worker-bulkextractor.tasks.bulkextractor"

# Task metadata for registration in the core system.
TASK_METADATA = {
    "display_name": "Bulkextractor",
    "description": "Runs the bulk_extractor command against a file",
    # Configuration that will be rendered as a web for in the UI, and any data entered
    # by the user will be available to the task function when executing (task_config).
}

def check_xml_attrib(xml_file, xml_key):
    """Checks if a key exists within the xml report.

        Args:
            xml_key(str): the xml key to check for.

        Returns:
            xml_hit(str): the xml value else return N/A.
        """
    xml_hit = 'N/A'
    xml_search = xml_file.find(xml_key)

    # If exists, return the text value.
    if xml_search is not None:
        xml_hit = xml_search.text
    return xml_hit

def generate_summary_report(output_dir):
    """Generate a summary report from the resulting bulk extractor run.

    Args:
        output_file_path(str): the path to the bulk extractor output.

    Returns:
        tuple: containing:
        report_test(str): The report data
        summary(str): A summary of the report (used for task status)
    """
    features_count = 0
    report_path = os.path.join(output_dir, 'report.xml')

    # Check if report.xml was not generated by bulk extractor.
    if not os.path.exists(report_path):
        report = 'Execution successful, but the report is not available.'
        return (report, report)

    # Parse existing XML file.
    xml_file = xml_tree.parse(report_path)
    report = Report("Bulk Extractor Results")

    # Place in try/except statement to continue execution when
    # an attribute is not found and NoneType is returned.
    try:
        # Retrieve summary related results.
        section = report.add_section()
        section.add_header("Run Summary")
        section.add_bullet(
                'Program: {0} - {1}'.format(
                    check_xml_attrib(xml_file,'creator/program'),
                    check_xml_attrib(xml_file,'creator/version')))
        section.add_bullet(
                'Command Line: {0}'.format(
                    check_xml_attrib(
                        xml_file,
                        'creator/execution_environment/command_line')))
        section.add_bullet(
                'Start Time: {0}'.format(
                    check_xml_attrib(
                        xml_file,
                        'creator/execution_environment/start_time')))
        section.add_bullet(
                f"Elapsed Time: {check_xml_attrib(xml_file, 'report/elapsed_seconds')}"
            )

        # Retrieve results from each of the scanner runs and display in table
        feature_files = xml_file.find(".//feature_files")
        scanner_results = []
        section = report.add_section()
        if feature_files is not None:
            section.add_header('Scanner Results\n')
            for name, count in zip(xml_file.findall(".//feature_file/name"),
                                    xml_file.findall(".//feature_file/count")):
                scanner_results.append({"Name": name.text, "Count": int(count.text)})
                features_count += int(count.text)
            sorted_scanner_results = sorted(
                scanner_results, key=lambda x: x["Count"], reverse=True)
            columns = scanner_results[0].keys()
            t = MarkdownTable(columns)
            for scanner_result in sorted_scanner_results:
                print([str(scanner_result[column]) for column in columns])
                t.add_row([str(scanner_result[column]) for column in columns])
            section.add_table(t)
        else:
            section.add_header("There are no findings to report.")
    except AttributeError as exception:
        raise exception
    report.summary = f'{features_count} artifacts have been extracted.'
    return report

def extract_non_empty_files(artifact_dir, output_path) -> List[Dict]:
    """Walks a firectory and returns a list of OutputFiles that are not empty
    
    Args:
        artifact_dir(str): The path of the directory to walk
        output_path(str): The path of the directory to store the results
    """
    out_dir = os.path.join(artifact_dir, "output")
    os.makedirs(out_dir, exist_ok=True)
    out_files = []
    for entry in glob.glob(os.path.join(artifact_dir, '**'), recursive=True):
        if os.path.exists(entry) and not os.path.isdir(entry):
            with open(entry, "rb") as f:
                content = f.read()
                if content:
                    out_file = create_output_file(output_path, display_name=os.path.basename(entry))
                    with open(out_file.path, "wb") as out_f:
                        out_f.write(content)
                    out_files.append(out_file.to_dict())
    return out_files

@celery.task(bind=True, name=TASK_NAME, metadata=TASK_METADATA)
def command(
    self,
    pipe_result: str = None,
    input_files: list = None,
    output_path: str = None,
    workflow_id: str = None,
    task_config: dict = None,
) -> str:
    """Run bulk_extractor on input files.
    Args:
        pipe_result: Base64-encoded result from the previous Celery task, if any.
        input_files: List of input file dictionaries (unused if pipe_result exists).
        output_path: Path to the output directory.
        workflow_id: ID of the workflow.
        task_config: User configuration for the task.

    Returns:
        Base64-encoded dictionary containing task results.
    """
    input_files = get_input_files(pipe_result, input_files or [])
    output_files = []
    file_reports = []

    for input_file in input_files:
        base_command = ["bulk_extractor"]
        report_file = create_output_file(
            output_path,
            display_name=f"Report_{input_file.get("display_name")}.html",
        )
        tmp_artifacts_dir = os.path.join(output_path, uuid4().hex)
        base_command.extend(["-o", tmp_artifacts_dir])
        base_command_string = " ".join(base_command)
        command = base_command + [input_file.get("path")]

        # Run the command
        process  = subprocess.Popen(command)
        process.wait()
        if process.returncode == 0:
            # Execution complete, verify the results
            if os.path.exists(tmp_artifacts_dir):
                report = generate_summary_report(tmp_artifacts_dir)
                with open(report_file.path, "w") as fh:
                    fh.write(report.to_markdown())
                output_files.append(report_file.to_dict())
                output_files.extend(extract_non_empty_files(tmp_artifacts_dir, output_path))
                file_reports.append(serialize_file_report(input_file, report_file, report))
            else:
                print("os.path.exists({}):{} when expected True".format(tmp_artifacts_dir, os.path.exists(tmp_artifacts_dir)))
                raise
        else:
            raise
        
        shutil.rmtree(tmp_artifacts_dir)

    if not output_files:
        raise RuntimeError("Error running bulk extractor, no files returned.")

    return create_task_result(
        output_files=output_files,
        workflow_id=workflow_id,
        command=base_command_string,
        meta={},
        file_reports=file_reports,
    )

