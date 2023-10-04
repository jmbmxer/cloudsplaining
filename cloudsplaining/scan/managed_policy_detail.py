"""Represents the Policies section of the output generated by the aws iam get-account-authorization-details command."""
# Copyright (c) 2020, salesforce.com, inc.
# All rights reserved.
# Licensed under the BSD 3-Clause license.
# For full license text, see the LICENSE file in the repo root
# or https://opensource.org/licenses/BSD-3-Clause
from __future__ import annotations

import logging
from typing import Dict, Any, List

from policy_sentry.util.arns import get_account_from_arn
from cloudsplaining.scan.policy_document import PolicyDocument
from cloudsplaining.shared.exceptions import NotFoundException
from cloudsplaining.shared.utils import get_full_policy_path, is_aws_managed
from cloudsplaining.shared.constants import ISSUE_SEVERITY, RISK_DEFINITION
from cloudsplaining.shared.exclusions import (
    DEFAULT_EXCLUSIONS,
    Exclusions,
    is_name_excluded,
)


logger = logging.getLogger(__name__)


class ManagedPolicyDetails:
    """
    Holds ManagedPolicy objects. This is sourced from the 'Policies' section of the Authz file - whether they are AWS managed or customer managed.
    """

    def __init__(
        self,
        policy_details: List[Dict[str, Any]],
        exclusions: Exclusions = DEFAULT_EXCLUSIONS,
        flag_conditional_statements: bool = False,
        flag_resource_arn_statements: bool = False,
        severity: List[Any] | None = None,
    ) -> None:
        self.policy_details = []
        if not isinstance(exclusions, Exclusions):
            raise Exception(
                "The exclusions provided is not an Exclusions type object. "
                "Please supply an Exclusions object and try again."
            )
        self.exclusions = exclusions
        self.flag_conditional_statements = flag_conditional_statements
        self.flag_resource_arn_statements = flag_resource_arn_statements
        self.iam_data: Dict[str, Dict[Any, Any]] = {
            "groups": {},
            "users": {},
            "roles": {},
        }

        for policy_detail in policy_details:
            this_policy_name = policy_detail["PolicyName"]
            this_policy_id = policy_detail["PolicyId"]
            this_policy_path = policy_detail["Path"]
            # Always exclude the AWS service role policies
            if is_name_excluded(
                this_policy_path, "aws-service-role*"
            ) or is_name_excluded(this_policy_path, "/aws-service-role*"):
                logger.debug(
                    "The %s Policy with the policy ID %s is excluded because it is "
                    "an immutable AWS Service role with a path of %s",
                    this_policy_name,
                    this_policy_id,
                    this_policy_path,
                )
                continue
            # Exclude the managed policies
            if (
                exclusions.is_policy_excluded(this_policy_name)
                or exclusions.is_policy_excluded(this_policy_id)
                or exclusions.is_policy_excluded(this_policy_path)
            ):
                logger.debug(
                    "The %s Managed Policy with the policy ID %s and %s path is excluded.",
                    this_policy_name,
                    this_policy_id,
                    this_policy_path,
                )
                continue
            self.policy_details.append(
                ManagedPolicy(
                    policy_detail,
                    exclusions=exclusions,
                    flag_resource_arn_statements=self.flag_resource_arn_statements,
                    flag_conditional_statements=self.flag_conditional_statements,
                    severity=severity,
                )
            )

    def get_policy_detail(self, arn: str) -> "ManagedPolicy":
        """Get a ManagedPolicy object by providing the ARN. This is useful to PrincipalDetail objects"""
        for policy_detail in self.policy_details:
            if policy_detail.arn == arn:
                return policy_detail
        raise NotFoundException(f"Managed Policy ARN {arn} not found.")

    @property
    def all_infrastructure_modification_actions(self) -> List[str]:
        """Return a list of all infrastructure modification actions allowed by all managed policies in violation."""
        result = set()
        for policy in self.policy_details:
            result.update(policy.policy_document.infrastructure_modification)
        return sorted(result)

    @property
    def json(self) -> Dict[str, Dict[str, Any]]:
        """Get all JSON results"""
        result = {policy.policy_id: policy.json for policy in self.policy_details}
        return result

    @property
    def json_large(self) -> Dict[str, Dict[str, Any]]:
        """Get all JSON results"""
        result = {policy.policy_id: policy.json_large for policy in self.policy_details}
        return result

    @property
    def json_large_aws_managed(self) -> Dict[str, Dict[str, Any]]:
        """Get all JSON results"""
        result = {
            policy.policy_id: policy.json_large
            for policy in self.policy_details
            if policy.managed_by == "AWS"
        }
        return result

    @property
    def json_large_customer_managed(self) -> Dict[str, Dict[str, Any]]:
        """Get all JSON results"""
        result = {
            policy.policy_id: policy.json_large
            for policy in self.policy_details
            if policy.managed_by == "Customer"
        }
        return result

    def set_iam_data(self, iam_data: Dict[str, Dict[Any, Any]]) -> None:
        self.iam_data = iam_data
        for policy_detail in self.policy_details:
            policy_detail.set_iam_data(iam_data)


# pylint: disable=too-many-instance-attributes
class ManagedPolicy:
    """
    Contains information about an IAM Managed Policy, including the Policy Document.

    https://docs.aws.amazon.com/IAM/latest/APIReference/API_PolicyDetail.html
    """

    def __init__(
        self,
        policy_detail: Dict[str, Any],
        exclusions: Exclusions = DEFAULT_EXCLUSIONS,
        flag_conditional_statements: bool = False,
        flag_resource_arn_statements: bool = False,
        severity: List[str] | None = None,
    ) -> None:
        # Store the Raw JSON data from this for safekeeping
        self.policy_detail = policy_detail

        self.flag_conditional_statements = flag_conditional_statements
        self.flag_resource_arn_statements = flag_resource_arn_statements

        # Store the attributes per Policy item
        self.policy_name = policy_detail["PolicyName"]
        self.policy_id = policy_detail["PolicyId"]
        self.arn = policy_detail["Arn"]
        self.path = policy_detail["Path"]
        self.default_version_id = policy_detail.get("DefaultVersionId")
        self.attachment_count = policy_detail.get("AttachmentCount")
        self.permissions_boundary_usage_count = policy_detail.get(
            "PermissionsBoundaryUsageCount"
        )
        self.is_attachable = policy_detail.get("IsAttachable")
        self.create_date = policy_detail.get("CreateDate")
        self.update_date = policy_detail.get("UpdateDate")
        self.iam_data: Dict[str, Dict[Any, Any]] = {
            "groups": {},
            "users": {},
            "roles": {},
        }

        if not isinstance(exclusions, Exclusions):
            raise Exception(
                "The exclusions provided is not an Exclusions type object. "
                "Please supply an Exclusions object and try again."
            )
        self.exclusions = exclusions
        self.is_excluded = self._is_excluded(exclusions)

        # Policy Documents are stored here. Multiple indices though. We will evaluate the one
        #   with IsDefaultVersion only.
        self.policy_version_list = policy_detail.get("PolicyVersionList", [])

        self.policy_document = self._policy_document()

        self.severity = [] if severity is None else severity

    def set_iam_data(self, iam_data: Dict[str, Dict[Any, Any]]) -> None:
        self.iam_data = iam_data

    def _is_excluded(self, exclusions: Exclusions) -> bool:
        """Determine whether the policy name or policy ID is excluded"""
        return (
            exclusions.is_policy_excluded(self.policy_name)
            or exclusions.is_policy_excluded(self.policy_id)
            or exclusions.is_policy_excluded(self.path)
            or is_name_excluded(self.path, "/aws-service-role*")
        )

    def _policy_document(self) -> PolicyDocument:
        """Return the policy document object"""
        for policy_version in self.policy_version_list:
            if policy_version.get("IsDefaultVersion") is True:
                return PolicyDocument(
                    policy_version.get("Document"),
                    exclusions=self.exclusions,
                    flag_resource_arn_statements=self.flag_resource_arn_statements,
                    flag_conditional_statements=self.flag_conditional_statements,
                )
        raise Exception(
            "Managed Policy ARN %s has no default Policy Document version", self.arn
        )

    # This will help with the Exclusions mechanism. Get the full path of the policy, including the name.
    @property
    def full_policy_path(self) -> str:
        """Get the full policy path, including /aws-service-role/, if applicable"""
        return get_full_policy_path(self.arn)

    @property
    def managed_by(self) -> str:  # pragma: no cover
        """Determine whether the policy is AWS-Managed or Customer-managed based on a Policy ARN pattern."""
        if is_aws_managed(self.arn):
            return "AWS"
        else:
            return "Customer"

    @property
    def account_id(self) -> str:  # pragma: no cover
        """Return the account ID"""
        if is_aws_managed(self.arn):
            return "N/A"
        else:
            return get_account_from_arn(self.arn)  # type: ignore

    def getFindingLinks(self, findings: List[Dict[str, Any]]) -> Dict[Any, str]:
        links = {}
        for finding in findings:
            links[
                finding["type"]
            ] = f'https://cloudsplaining.readthedocs.io/en/latest/glossary/privilege-escalation/#{finding["type"]}'
        return links

    @property
    def getAttached(self) -> Dict[str, Any]:
        attached: Dict[str, Any] = {"roles": [], "groups": [], "users": []}
        for principalType in ["roles", "groups", "users"]:
            principals = (self.iam_data[principalType]).keys()
            for principalID in principals:
                managedPolicies = {}
                if self.is_excluded:
                    return {}
                if self.managed_by == "AWS":
                    managedPolicies.update(
                        self.iam_data[principalType][principalID][
                            "aws_managed_policies"
                        ]
                    )
                elif self.managed_by == "Customer":
                    managedPolicies.update(
                        self.iam_data[principalType][principalID][
                            "customer_managed_policies"
                        ]
                    )
                if self.policy_id in managedPolicies:
                    attached[principalType].append(
                        self.iam_data[principalType][principalID]["name"]
                    )
        return attached

    @property
    def json(self) -> Dict[str, Any]:
        """Return JSON output for high risk actions"""
        result = dict(
            PolicyName=self.policy_name,
            PolicyId=self.policy_id,
            Arn=self.arn,
            Path=self.path,
            DefaultVersionId=self.default_version_id,
            AttachmentCount=self.attachment_count,
            AttachedTo=self.getAttached,
            IsAttachable=self.is_attachable,
            CreateDate=self.create_date,
            UpdateDate=self.update_date,
            PolicyVersionList=self.policy_version_list,
            PrivilegeEscalation={
                "severity": ISSUE_SEVERITY["PrivilegeEscalation"],
                "description": RISK_DEFINITION["PrivilegeEscalation"],
                "findings": self.policy_document.allows_privilege_escalation
                if ISSUE_SEVERITY["PrivilegeEscalation"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
                "links": self.getFindingLinks(
                    self.policy_document.allows_privilege_escalation
                    if ISSUE_SEVERITY["PrivilegeEscalation"]
                    in [x.lower() for x in self.severity]
                    or not self.severity
                    else []
                ),
            },
            DataExfiltration={
                "severity": ISSUE_SEVERITY["DataExfiltration"],
                "description": RISK_DEFINITION["DataExfiltration"],
                "findings": self.policy_document.allows_data_exfiltration_actions
                if ISSUE_SEVERITY["DataExfiltration"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            ResourceExposure={
                "severity": ISSUE_SEVERITY["ResourceExposure"],
                "description": RISK_DEFINITION["ResourceExposure"],
                "findings": self.policy_document.permissions_management_without_constraints
                if ISSUE_SEVERITY["ResourceExposure"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            ServiceWildcard={
                "severity": ISSUE_SEVERITY["ServiceWildcard"],
                "description": RISK_DEFINITION["ServiceWildcard"],
                "findings": self.policy_document.service_wildcard
                if ISSUE_SEVERITY["ServiceWildcard"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            CredentialsExposure={
                "severity": ISSUE_SEVERITY["CredentialsExposure"],
                "description": RISK_DEFINITION["CredentialsExposure"],
                "findings": self.policy_document.credentials_exposure
                if ISSUE_SEVERITY["CredentialsExposure"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            is_excluded=self.is_excluded,
        )
        return result

    @property
    def json_large(self) -> Dict[str, Any]:
        """Return JSON output - including Infra Modification actions, which can be large"""
        result = dict(
            PolicyName=self.policy_name,
            PolicyId=self.policy_id,
            Arn=self.arn,
            Path=self.path,
            DefaultVersionId=self.default_version_id,
            AttachmentCount=self.attachment_count,
            AttachedTo=self.getAttached,
            IsAttachable=self.is_attachable,
            CreateDate=self.create_date,
            UpdateDate=self.update_date,
            PolicyVersionList=self.policy_version_list,
            PrivilegeEscalation={
                "severity": ISSUE_SEVERITY["PrivilegeEscalation"],
                "description": RISK_DEFINITION["PrivilegeEscalation"],
                "findings": self.policy_document.allows_privilege_escalation
                if ISSUE_SEVERITY["PrivilegeEscalation"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
                "links": self.getFindingLinks(
                    self.policy_document.allows_privilege_escalation
                    if ISSUE_SEVERITY["PrivilegeEscalation"]
                    in [x.lower() for x in self.severity]
                    or not self.severity
                    else []
                ),
            },
            DataExfiltration={
                "severity": ISSUE_SEVERITY["DataExfiltration"],
                "description": RISK_DEFINITION["DataExfiltration"],
                "findings": self.policy_document.allows_data_exfiltration_actions
                if ISSUE_SEVERITY["DataExfiltration"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            ResourceExposure={
                "severity": ISSUE_SEVERITY["ResourceExposure"],
                "description": RISK_DEFINITION["ResourceExposure"],
                "findings": self.policy_document.permissions_management_without_constraints
                if ISSUE_SEVERITY["ResourceExposure"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            ServiceWildcard={
                "severity": ISSUE_SEVERITY["ServiceWildcard"],
                "description": RISK_DEFINITION["ServiceWildcard"],
                "findings": self.policy_document.service_wildcard
                if ISSUE_SEVERITY["ServiceWildcard"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            CredentialsExposure={
                "severity": ISSUE_SEVERITY["CredentialsExposure"],
                "description": RISK_DEFINITION["CredentialsExposure"],
                "findings": self.policy_document.credentials_exposure
                if ISSUE_SEVERITY["CredentialsExposure"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            InfrastructureModification={
                "severity": ISSUE_SEVERITY["InfrastructureModification"],
                "description": RISK_DEFINITION["InfrastructureModification"],
                "findings": self.policy_document.infrastructure_modification
                if ISSUE_SEVERITY["InfrastructureModification"]
                in [x.lower() for x in self.severity]
                or not self.severity
                else [],
            },
            is_excluded=self.is_excluded,
        )
        return result
