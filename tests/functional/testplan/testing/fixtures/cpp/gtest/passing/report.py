from testplan.report import TestReport, TestGroupReport, TestCaseReport

expected_report = TestReport(
    name="plan",
    entries=[
        TestGroupReport(
            name="My GTest",
            category="gtest",
            entries=[
                TestGroupReport(
                    name="SquareRootTest",
                    category="testsuite",
                    entries=[
                        TestCaseReport(
                            name="PositiveNos",
                            entries=[
                                {"type": "RawAssertion", "passed": True},
                            ],
                        ),
                        TestCaseReport(
                            name="NegativeNos",
                            entries=[
                                {"type": "RawAssertion", "passed": True},
                            ],
                        ),
                    ],
                ),
                TestGroupReport(
                    name="SquareRootTestNonFatal",
                    category="testsuite",
                    entries=[
                        TestCaseReport(
                            name="PositiveNos",
                            entries=[
                                {"type": "RawAssertion", "passed": True},
                            ],
                        ),
                        TestCaseReport(
                            name="NegativeNos",
                            entries=[
                                {"type": "RawAssertion", "passed": True},
                            ],
                        ),
                    ],
                ),
                TestGroupReport(
                    name="ProcessChecks",
                    category="testsuite",
                    entries=[
                        TestCaseReport(
                            name="ExitCodeCheck",
                            entries=[
                                {"type": "RawAssertion", "passed": True},
                                {
                                    "type": "Attachment",
                                    "description": "Process stdout",
                                },
                                {
                                    "type": "Attachment",
                                    "description": "Process stderr",
                                },
                            ],
                        ),
                    ],
                ),
            ],
        )
    ],
)
