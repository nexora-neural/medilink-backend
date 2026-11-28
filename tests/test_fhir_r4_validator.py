"""Optional validation of synthetic export fixtures using the official HL7 CLI."""

import os
import subprocess
from pathlib import Path
from unittest import TestCase, skipUnless

VALIDATOR_JAR = os.getenv("FHIR_VALIDATOR_JAR")
VALIDATOR_HOME = os.getenv("FHIR_VALIDATOR_HOME")
FHIR_FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures" / "fhir-r4"


@skipUnless(VALIDATOR_JAR, "Set FHIR_VALIDATOR_JAR to the official HL7 FHIR Validator CLI jar.")
class FhirR4StructureValidationTests(TestCase):
    def test_representative_patient_and_medication_request_validate_as_r4(self):
        jar = Path(VALIDATOR_JAR)
        self.assertTrue(jar.is_file(), "FHIR_VALIDATOR_JAR must point to a readable jar file.")

        for fixture in sorted(FHIR_FIXTURE_DIRECTORY.glob("*.json")):
            with self.subTest(resource=fixture.name):
                command = ["java"]
                if VALIDATOR_HOME:
                    command.append(f"-Duser.home={VALIDATOR_HOME}")
                command.extend(
                    ["-jar", str(jar), str(fixture), "-version", "4.0.1", "-tx", "n/a"]
                )
                result = subprocess.run(
                    command,
                    capture_output=True,
                    check=False,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    f"FHIR R4 validation failed for {fixture.name}:\n"
                    f"{result.stdout}\n{result.stderr}",
                )
