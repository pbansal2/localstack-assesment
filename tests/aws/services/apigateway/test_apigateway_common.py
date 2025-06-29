import json
import textwrap
import time
from operator import itemgetter

import pytest
import requests
from botocore.exceptions import ClientError

from localstack.aws.api.lambda_ import Runtime
from localstack.constants import TAG_KEY_CUSTOM_ID
from localstack.testing.aws.util import is_aws_cloud
from localstack.testing.pytest import markers
from localstack.utils.aws.arns import get_partition, parse_arn
from localstack.utils.strings import short_uid
from localstack.utils.sync import retry
from tests.aws.services.apigateway.apigateway_fixtures import (
    api_invoke_url,
    create_rest_api_deployment,
    create_rest_api_integration,
    create_rest_api_stage,
    create_rest_resource_method,
)
from tests.aws.services.apigateway.conftest import APIGATEWAY_ASSUME_ROLE_POLICY, is_next_gen_api
from tests.aws.services.lambda_.test_lambda import TEST_LAMBDA_AWS_PROXY


def _create_mock_integration_with_200_response_template(
    aws_client, api_id: str, resource_id: str, http_method: str, response_template: dict
):
    aws_client.apigateway.put_method(
        restApiId=api_id,
        resourceId=resource_id,
        httpMethod=http_method,
        authorizationType="NONE",
    )

    aws_client.apigateway.put_method_response(
        restApiId=api_id,
        resourceId=resource_id,
        httpMethod=http_method,
        statusCode="200",
    )

    aws_client.apigateway.put_integration(
        restApiId=api_id,
        resourceId=resource_id,
        httpMethod=http_method,
        type="MOCK",
        requestTemplates={"application/json": '{"statusCode": 200}'},
    )

    aws_client.apigateway.put_integration_response(
        restApiId=api_id,
        resourceId=resource_id,
        httpMethod=http_method,
        statusCode="200",
        selectionPattern="",
        responseTemplates={"application/json": json.dumps(response_template)},
    )


class TestApiGatewayCommon:
    """
    In this class we won't test individual CRUD API calls but how those will affect the integrations and
    requests/responses from the API.
    """

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$.invalid-request-body.Type",
            "$.missing-required-qs-request-params-get.Type",
            "$.missing-required-headers-request-params-get.Type",
            "$.missing-all-required-request-params-post.Type",
        ]
    )
    def test_api_gateway_request_validator(
        self, create_lambda_function, create_rest_apigw, apigw_redeploy_api, snapshot, aws_client
    ):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("requestValidatorId"),
                snapshot.transform.key_value("cacheNamespace"),
                snapshot.transform.key_value("id"),  # deployment id
                snapshot.transform.key_value("fn_name"),  # lambda name
                snapshot.transform.key_value("fn_arn"),  # lambda arn
            ]
        )

        fn_name = f"test-{short_uid()}"
        create_lambda_function(
            func_name=fn_name,
            handler_file=TEST_LAMBDA_AWS_PROXY,
            runtime=Runtime.python3_12,
        )
        lambda_arn = aws_client.lambda_.get_function(FunctionName=fn_name)["Configuration"][
            "FunctionArn"
        ]
        # matching on lambda id for reference replacement in snapshots
        snapshot.match("register-lambda", {"fn_name": fn_name, "fn_arn": lambda_arn})

        parsed_arn = parse_arn(lambda_arn)
        region = parsed_arn["region"]
        account_id = parsed_arn["account"]

        api_id, _, root = create_rest_apigw(name="aws lambda api")

        resource_1 = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root, pathPart="nested"
        )["id"]

        resource_id = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=resource_1, pathPart="{test}"
        )["id"]

        validator_id = aws_client.apigateway.create_request_validator(
            restApiId=api_id,
            name="test-validator",
            validateRequestParameters=True,
            validateRequestBody=True,
        )["id"]

        # create Model schema to validate body
        aws_client.apigateway.create_model(
            restApiId=api_id,
            name="testSchema",
            contentType="application/json",
            schema=json.dumps(
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "title": "testSchema",
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
                }
            ),
        )

        for http_method in ("GET", "POST"):
            aws_client.apigateway.put_method(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod=http_method,
                authorizationType="NONE",
                requestValidatorId=validator_id,
                requestParameters={
                    # the path parameter is most often used to generate SDK from the REST API
                    "method.request.path.test": True,
                    "method.request.querystring.qs1": True,
                    "method.request.header.x-header-param": True,
                },
                requestModels={"application/json": "testSchema"},
            )

            aws_client.apigateway.put_integration(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod=http_method,
                integrationHttpMethod="POST",
                type="AWS_PROXY",
                uri=f"arn:{get_partition(region)}:apigateway:{region}:lambda:path/2015-03-31/functions/{lambda_arn}/invocations",
            )
            aws_client.apigateway.put_method_response(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod=http_method,
                statusCode="200",
            )
            aws_client.apigateway.put_integration_response(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod=http_method,
                statusCode="200",
            )

        stage_name = "local"
        deploy_1 = aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)
        snapshot.match("deploy-1", deploy_1)

        source_arn = (
            f"arn:{get_partition(region)}:execute-api:{region}:{account_id}:{api_id}/*/*/nested/*"
        )

        aws_client.lambda_.add_permission(
            FunctionName=lambda_arn,
            StatementId=str(short_uid()),
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=source_arn,
        )

        url = api_invoke_url(api_id, stage=stage_name, path="/nested/value")
        # test that with every request parameters and a valid body, it passes
        response = requests.post(
            url,
            json={"a": 1, "b": 2},
            headers={"x-header-param": "test"},
            params={"qs1": "test"},
        )
        assert response.ok
        assert json.loads(response.json()["body"]) == {"a": 1, "b": 2}

        # GET request with no body
        response_get = requests.get(
            url,
            headers={"x-header-param": "test"},
            params={"qs1": "test"},
        )
        assert response_get.status_code == 400

        # replace the POST method requestParameters to require a non-existing {issuer} path part
        response = aws_client.apigateway.update_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            patchOperations=[
                {
                    "op": "add",
                    "path": "/requestParameters/method.request.path.issuer",
                    "value": "true",
                },
                {
                    "op": "remove",
                    "path": "/requestParameters/method.request.path.test",
                    "value": "true",
                },
            ],
        )
        snapshot.match("change-request-path-names", response)
        apigw_redeploy_api(rest_api_id=api_id, stage_name=stage_name)

        response = requests.post(url, json={"test": "test"})
        assert response.status_code == 400
        snapshot.match("missing-all-required-request-params-post", response.json())

        response = requests.get(url, params={"qs1": "test"})
        assert response.status_code == 400
        snapshot.match("missing-required-headers-request-params-get", response.json())

        response = requests.get(url, headers={"x-header-param": "test"})
        assert response.status_code == 400
        snapshot.match("missing-required-qs-request-params-get", response.json())

        # revert the path validation for POST method
        response = aws_client.apigateway.update_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            patchOperations=[
                {
                    "op": "add",
                    "path": "/requestParameters/method.request.path.test",
                    "value": "true",
                },
                {
                    "op": "remove",
                    "path": "/requestParameters/method.request.path.issuer",
                    "value": "true",
                },
            ],
        )
        snapshot.match("revert-request-path-names", response)
        apigw_redeploy_api(rest_api_id=api_id, stage_name=stage_name)
        retries = 10 if is_aws_cloud() else 3
        sleep_time = 10 if is_aws_cloud() else 1

        def _wrong_path_removed():
            # the validator should work with a valid object
            _response = requests.post(
                url,
                json={"a": 1, "b": 2},
                headers={"x-header-param": "test"},
                params={"qs1": "test"},
            )
            assert _response.status_code == 200

        retry(_wrong_path_removed, retries=retries, sleep=sleep_time)

        def _invalid_body():
            # the validator should fail with this message not respecting the schema
            _response = requests.post(
                url,
                json={"test": "test"},
                headers={"x-header-param": "test"},
                params={"qs1": "test"},
            )
            assert _response.status_code == 400
            content = _response.json()
            assert content["message"] == "Invalid request body"
            return content

        response_content = retry(_invalid_body, retries=retries, sleep=sleep_time)
        snapshot.match("invalid-request-body", response_content)

        # GET request with an empty body
        response_get = requests.get(
            url,
            headers={"x-header-param": "test"},
            params={"qs1": "test"},
        )
        assert response_get.status_code == 400
        assert response_get.json()["message"] == "Invalid request body"

        # GET request with an empty body, content type JSON
        response_get = requests.get(
            url,
            headers={"Content-Type": "application/json", "x-header-param": "test"},
            params={"qs1": "test"},
        )
        assert response_get.status_code == 400

        # update request validator to disable validation
        patch_operations = [
            {"op": "replace", "path": "/validateRequestBody", "value": "false"},
            {"op": "replace", "path": "/validateRequestParameters", "value": "false"},
        ]
        response = aws_client.apigateway.update_request_validator(
            restApiId=api_id, requestValidatorId=validator_id, patchOperations=patch_operations
        )
        snapshot.match("disable-request-validator", response)
        apigw_redeploy_api(rest_api_id=api_id, stage_name=stage_name)

        def _disabled_validation():
            _response = requests.post(url, json={"test": "test"})
            assert _response.ok
            return _response.json()

        response = retry(_disabled_validation, retries=retries, sleep=sleep_time)
        assert json.loads(response["body"]) == {"test": "test"}

        # GET request with an empty body
        response_get = requests.get(url)
        assert response_get.ok

    @markers.aws.validated
    def test_api_gateway_request_validator_with_ref_models(
        self, create_rest_apigw, apigw_redeploy_api, snapshot, aws_client
    ):
        api_id, _, root = create_rest_apigw(name="test ref models")

        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("id"),
                snapshot.transform.regex(api_id, "<api-id>"),
            ]
        )

        resource_id = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root, pathPart="path"
        )["id"]

        validator_id = aws_client.apigateway.create_request_validator(
            restApiId=api_id,
            name="test-validator",
            validateRequestParameters=True,
            validateRequestBody=True,
        )["id"]

        # create nested Model schema to validate body
        aws_client.apigateway.create_model(
            restApiId=api_id,
            name="testSchema",
            contentType="application/json",
            schema=json.dumps(
                {
                    "$schema": "http://json-schema.org/draft-04/schema#",
                    "title": "testSchema",
                    "type": "object",
                    "properties": {
                        "a": {"type": "number"},
                        "b": {"type": "number"},
                    },
                    "required": ["a", "b"],
                }
            ),
        )

        aws_client.apigateway.create_model(
            restApiId=api_id,
            name="testSchemaList",
            contentType="application/json",
            schema=json.dumps(
                {
                    "type": "array",
                    "items": {
                        # hardcoded URL to AWS
                        "$ref": f"https://apigateway.amazonaws.com/restapis/{api_id}/models/testSchema"
                    },
                }
            ),
        )

        get_models = aws_client.apigateway.get_models(restApiId=api_id)
        get_models["items"] = sorted(get_models["items"], key=itemgetter("name"))
        snapshot.match("get-models-with-ref", get_models)

        aws_client.apigateway.put_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            authorizationType="NONE",
            requestValidatorId=validator_id,
            requestModels={"application/json": "testSchemaList"},
        )

        aws_client.apigateway.put_method_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            statusCode="200",
        )

        aws_client.apigateway.put_integration(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            type="MOCK",
            requestTemplates={"application/json": '{"statusCode": 200}'},
        )

        aws_client.apigateway.put_integration_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            statusCode="200",
            selectionPattern="",
            responseTemplates={"application/json": json.dumps({"data": "ok"})},
        )

        stage_name = "local"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        url = api_invoke_url(api_id, stage=stage_name, path="/path")

        def invoke_api(_data: dict) -> dict:
            _response = requests.post(url, verify=False, json=_data)
            assert _response.ok
            content = _response.json()
            return content

        # test that with every request parameters and a valid body, it passes
        response = retry(
            invoke_api, retries=10 if is_aws_cloud() else 3, sleep=1, _data=[{"a": 1, "b": 2}]
        )
        snapshot.match("successful", response)

        response_post_no_body = requests.post(url)
        assert response_post_no_body.status_code == 400
        snapshot.match("failed-validation", response_post_no_body.json())

    @markers.aws.validated
    def test_api_gateway_request_validator_with_ref_one_ofmodels(
        self, create_rest_apigw, apigw_redeploy_api, snapshot, aws_client
    ):
        api_id, _, root = create_rest_apigw(name="test oneOf ref models")

        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("id"),
                snapshot.transform.regex(api_id, "<api-id>"),
            ]
        )

        resource_id = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root, pathPart="path"
        )["id"]

        validator_id = aws_client.apigateway.create_request_validator(
            restApiId=api_id,
            name="test-validator",
            validateRequestParameters=True,
            validateRequestBody=True,
        )["id"]

        aws_client.apigateway.create_model(
            restApiId=api_id,
            name="StatusModel",
            contentType="application/json",
            schema=json.dumps(
                {
                    "type": "object",
                    "properties": {"Status": {"type": "string"}, "Order": {"type": "integer"}},
                    "required": [
                        "Status",
                        "Order",
                    ],
                }
            ),
        )

        aws_client.apigateway.create_model(
            restApiId=api_id,
            name="TestModel",
            contentType="application/json",
            schema=json.dumps(
                {
                    "type": "object",
                    "properties": {
                        "status": {
                            "oneOf": [
                                {"type": "null"},
                                {
                                    "$ref": f"https://apigateway.amazonaws.com/restapis/{api_id}/models/StatusModel"
                                },
                            ]
                        },
                    },
                    "required": ["status"],
                }
            ),
        )

        get_models = aws_client.apigateway.get_models(restApiId=api_id)
        get_models["items"] = sorted(get_models["items"], key=itemgetter("name"))
        snapshot.match("get-models-with-ref", get_models)

        aws_client.apigateway.put_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            authorizationType="NONE",
            requestValidatorId=validator_id,
            requestModels={"application/json": "TestModel"},
        )

        aws_client.apigateway.put_method_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            statusCode="200",
        )

        aws_client.apigateway.put_integration(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            type="MOCK",
            requestTemplates={"application/json": '{"statusCode": 200}'},
        )

        aws_client.apigateway.put_integration_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="POST",
            statusCode="200",
            selectionPattern="",
            responseTemplates={"application/json": json.dumps({"data": "ok"})},
        )

        stage_name = "local"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        url = api_invoke_url(api_id, stage=stage_name, path="/path")

        def invoke_api(_data: dict) -> dict:
            _response = requests.post(url, verify=False, json=_data)
            assert _response.ok
            content = _response.json()
            return content

        # test that with every request parameters and a valid body, it passes
        response = retry(
            invoke_api, retries=10 if is_aws_cloud() else 3, sleep=1, _data={"status": None}
        )
        snapshot.match("successful", response)

        response = invoke_api({"status": {"Status": "works", "Order": 1}})
        snapshot.match("successful-with-data", response)

        response_post_no_body = requests.post(url)
        assert response_post_no_body.status_code == 400
        snapshot.match("failed-validation-no-data", response_post_no_body.json())

        response_post_bad_body = requests.post(url, json={"badFormat": "bla"})
        assert response_post_bad_body.status_code == 400
        snapshot.match("failed-validation-bad-data", response_post_bad_body.json())

    @markers.aws.validated
    def test_integration_request_parameters_mapping(
        self, create_rest_apigw, aws_client, echo_http_server_post
    ):
        api_id, _, root = create_rest_apigw(
            name=f"test-api-{short_uid()}",
            description="this is my api",
        )

        create_rest_resource_method(
            aws_client.apigateway,
            restApiId=api_id,
            resourceId=root,
            httpMethod="GET",
            authorizationType="none",
            requestParameters={
                "method.request.header.customHeader": False,
            },
        )

        aws_client.apigateway.put_method_response(
            restApiId=api_id, resourceId=root, httpMethod="GET", statusCode="200"
        )

        create_rest_api_integration(
            aws_client.apigateway,
            restApiId=api_id,
            resourceId=root,
            httpMethod="GET",
            integrationHttpMethod="POST",
            type="HTTP",
            uri=echo_http_server_post,
            requestParameters={
                "integration.request.header.testHeader": "method.request.header.customHeader",
                "integration.request.header.contextHeader": "context.resourceId",
            },
        )

        aws_client.apigateway.put_integration_response(
            restApiId=api_id,
            resourceId=root,
            httpMethod="GET",
            statusCode="200",
            selectionPattern="2\\d{2}",
            responseTemplates={},
        )

        deployment_id, _ = create_rest_api_deployment(aws_client.apigateway, restApiId=api_id)
        create_rest_api_stage(
            aws_client.apigateway, restApiId=api_id, stageName="dev", deploymentId=deployment_id
        )

        invocation_url = api_invoke_url(api_id=api_id, stage="dev", path="/")

        def invoke_api(url):
            _response = requests.get(url, verify=False, headers={"customHeader": "test"})
            assert _response.ok
            content = _response.json()
            return content

        response_data = retry(invoke_api, sleep=2, retries=10, url=invocation_url)
        lower_case_headers = {k.lower(): v for k, v in response_data["headers"].items()}
        assert lower_case_headers["contextheader"] == root
        assert lower_case_headers["testheader"] == "test"

    @markers.aws.validated
    @pytest.mark.skipif(
        condition=not is_next_gen_api() and not is_aws_cloud(),
        reason="Wrong behavior in legacy implementation",
    )
    @markers.snapshot.skip_snapshot_verify(
        paths=[
            "$..server",
            "$..via",
            "$..x-amz-cf-id",
            "$..x-amz-cf-pop",
            "$..x-cache",
        ]
    )
    def test_invocation_trace_id(
        self,
        aws_client,
        create_rest_apigw,
        create_lambda_function,
        create_role_with_policy,
        region_name,
        snapshot,
    ):
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("via"),
                snapshot.transform.key_value("x-amz-cf-id"),
                snapshot.transform.key_value("x-amz-cf-pop"),
                snapshot.transform.key_value("x-amz-apigw-id"),
                snapshot.transform.key_value("x-amzn-trace-id"),
                snapshot.transform.key_value("FunctionName"),
                snapshot.transform.key_value("FunctionArn"),
                snapshot.transform.key_value("date", reference_replacement=False),
                snapshot.transform.key_value("content-length", reference_replacement=False),
            ]
        )
        api_id, _, root_id = create_rest_apigw(name="test trace id")

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="path"
        )
        hardcoded_resource_id = resource["id"]

        response_template_get = {"statusCode": 200}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, hardcoded_resource_id, "GET", response_template_get
        )

        fn_name = f"test-trace-id-{short_uid()}"
        # create lambda
        create_function_response = create_lambda_function(
            func_name=fn_name,
            handler_file=TEST_LAMBDA_AWS_PROXY,
            handler="lambda_aws_proxy.handler",
            runtime=Runtime.python3_12,
        )
        # create invocation role
        _, role_arn = create_role_with_policy(
            "Allow", "lambda:InvokeFunction", json.dumps(APIGATEWAY_ASSUME_ROLE_POLICY), "*"
        )
        lambda_arn = create_function_response["CreateFunctionResponse"]["FunctionArn"]
        # matching on lambda id for reference replacement in snapshots
        snapshot.match("register-lambda", {"FunctionName": fn_name, "FunctionArn": lambda_arn})

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="{proxy+}"
        )
        proxy_resource_id = resource["id"]

        aws_client.apigateway.put_method(
            restApiId=api_id,
            resourceId=proxy_resource_id,
            httpMethod="ANY",
            authorizationType="NONE",
        )

        # Lambda AWS_PROXY integration
        aws_client.apigateway.put_integration(
            restApiId=api_id,
            resourceId=proxy_resource_id,
            httpMethod="ANY",
            type="AWS_PROXY",
            integrationHttpMethod="POST",
            uri=f"arn:aws:apigateway:{region_name}:lambda:path/2015-03-31/functions/{lambda_arn}/invocations",
            credentials=role_arn,
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        def _invoke_api(path: str, headers: dict[str, str]) -> dict[str, str]:
            url = api_invoke_url(api_id=api_id, stage=stage_name, path=path)
            _response = requests.get(url, headers=headers)
            assert _response.ok
            lower_case_headers = {k.lower(): v for k, v in _response.headers.items()}
            return lower_case_headers

        retries = 10 if is_aws_cloud() else 3
        sleep = 3 if is_aws_cloud() else 1
        resp_headers = retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            headers={},
            path="/path",
        )

        snapshot.match("normal-req-headers-MOCK", resp_headers)
        assert "x-amzn-trace-id" not in resp_headers

        full_trace = "Root=1-3152b799-8954dae64eda91bc9a23a7e8;Parent=7fa8c0f79203be72;Sampled=1"
        trace_id = "Root=1-3152b799-8954dae64eda91bc9a23a7e8"
        hardcoded_parent = "Parent=7fa8c0f79203be72"

        resp_headers_with_trace_id = _invoke_api(
            path="/path", headers={"x-amzn-trace-id": full_trace}
        )
        snapshot.match("trace-id-req-headers-MOCK", resp_headers_with_trace_id)

        resp_proxy_headers = retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            headers={},
            path="/proxy-value",
        )
        snapshot.match("normal-req-headers-AWS_PROXY", resp_proxy_headers)

        resp_headers_with_trace_id = _invoke_api(
            path="/proxy-value", headers={"x-amzn-trace-id": full_trace}
        )
        snapshot.match("trace-id-req-headers-AWS_PROXY", resp_headers_with_trace_id)
        assert full_trace in resp_headers_with_trace_id["x-amzn-trace-id"]
        split_trace = resp_headers_with_trace_id["x-amzn-trace-id"].split(";")
        assert split_trace[1] == hardcoded_parent

        small_trace = trace_id
        resp_headers_with_trace_id = _invoke_api(
            path="/proxy-value", headers={"x-amzn-trace-id": small_trace}
        )
        snapshot.match("trace-id-small-req-headers-AWS_PROXY", resp_headers_with_trace_id)
        assert small_trace in resp_headers_with_trace_id["x-amzn-trace-id"]
        split_trace = resp_headers_with_trace_id["x-amzn-trace-id"].split(";")
        # assert that AWS populated the parent part of the trace with a generated one
        assert split_trace[1] != hardcoded_parent

    @markers.aws.validated
    def test_input_path_template_formatting(
        self, aws_client, create_rest_apigw, echo_http_server_post, snapshot
    ):
        api_id, _, root_id = create_rest_apigw()

        def _create_route(path: str, response_templates):
            resource_id = aws_client.apigateway.create_resource(
                restApiId=api_id, parentId=root_id, pathPart=path
            )["id"]
            aws_client.apigateway.put_method(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                authorizationType="NONE",
                apiKeyRequired=False,
            )

            aws_client.apigateway.put_method_response(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                statusCode="200",
            )

            aws_client.apigateway.put_integration(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                integrationHttpMethod="POST",
                type="HTTP",
                uri=echo_http_server_post,
            )

            aws_client.apigateway.put_integration_response(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                statusCode="200",
                selectionPattern="",
                responseTemplates={"application/json": response_templates},
            )

        _create_route("path", '#set($result = $input.path("$.json"))$result')
        _create_route("nested", '#set($result = $input.path("$.json"))$result.nested')
        _create_route("list", '#set($result = $input.path("$.json"))$result[0]')
        _create_route("to-string", '#set($result = $input.path("$.json"))$result.toString()')
        _create_route(
            "invalid-path",
            '#set($result = $input.path("$.nonExisting")){"body": $result, "nested": $result.nested, "isNull": #if( $result == $null )"true"#else"false"#end, "isEmptyString": #if( $result == "" )"true"#else"false"#end}',
        )
        _create_route(
            "nested-list",
            '#set($result = $input.path("$.json.listValue")){"body": $result, "nested": $result.nested, "isNull": #if( $result == $null )"true"#else"false"#end, "isEmptyString": #if( $result == "" )"true"#else"false"#end}',
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        url = api_invoke_url(api_id=api_id, stage=stage_name, path="/")
        path_url = url + "path"
        nested_url = url + "nested"
        list_url = url + "list"
        to_string = url + "to-string"
        invalid_path = url + "invalid-path"
        nested_list = url + "nested-list"

        response = requests.post(path_url, json={"foo": "bar"})
        snapshot.match("dict-response", response.text)

        response = requests.post(path_url, json=[{"foo": "bar"}])
        snapshot.match("json-list", response.text)

        response = requests.post(nested_url, json={"nested": {"foo": "bar"}})
        snapshot.match("nested-dict", response.text)

        response = requests.post(nested_url, json={"nested": [{"foo": "bar"}]})
        snapshot.match("nested-list", response.text)

        response = requests.post(list_url, json=[{"foo": "bar"}])
        snapshot.match("dict-in-list", response.text)

        response = requests.post(list_url, json=[[{"foo": "bar"}]])
        snapshot.match("list-with-nested-list", response.text)

        response = requests.post(path_url, json={"foo": [{"nested": "bar"}]})
        snapshot.match("dict-with-nested-list", response.text)

        response = requests.post(
            path_url, json={"bigger": "dict", "to": "test", "with": "separators"}
        )
        snapshot.match("bigger-dict", response.text)

        response = requests.post(to_string, json={"foo": "bar"})
        snapshot.match("to-string", response.text)

        response = requests.post(to_string, json={"list": [{"foo": "bar"}]})
        snapshot.match("list-to-string", response.text)

        response = requests.post(invalid_path)
        snapshot.match("empty-body", response.text)

        response = requests.post(nested_list, json={"listValue": []})
        snapshot.match("nested-empty-list", response.text)

        response = requests.post(nested_list, json={"listValue": None})
        snapshot.match("nested-null-list", response.text)

    @markers.aws.validated
    def test_input_body_formatting(
        self, aws_client, create_lambda_function, create_rest_apigw, snapshot
    ):
        api_id, _, root_id = create_rest_apigw()

        # create a special lambda URL returning exactly what it got as a body
        handler_code = handler_code = textwrap.dedent("""
        def handler(event, context):
            return event.get("body", "")
        """)
        func_name = f"echo-http-{short_uid()}"
        create_lambda_function(
            func_name=func_name,
            handler_file=handler_code,
            runtime=Runtime.python3_12,
        )
        url_response = aws_client.lambda_.create_function_url_config(
            FunctionName=func_name, AuthType="NONE"
        )
        aws_client.lambda_.add_permission(
            FunctionName=func_name,
            StatementId="urlPermission",
            Action="lambda:InvokeFunctionUrl",
            Principal="*",
            FunctionUrlAuthType="NONE",
        )
        echo_endpoint_url = url_response["FunctionUrl"]

        def _create_route(path: str, request_template: str, response_template: str):
            resource_id = aws_client.apigateway.create_resource(
                restApiId=api_id, parentId=root_id, pathPart=path
            )["id"]
            aws_client.apigateway.put_method(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                authorizationType="NONE",
                apiKeyRequired=False,
            )

            aws_client.apigateway.put_method_response(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                statusCode="200",
            )

            aws_client.apigateway.put_integration(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                integrationHttpMethod="POST",
                type="HTTP",
                uri=echo_endpoint_url,
                requestTemplates={"application/json": request_template},
            )

            aws_client.apigateway.put_integration_response(
                restApiId=api_id,
                resourceId=resource_id,
                httpMethod="POST",
                statusCode="200",
                selectionPattern="",
                responseTemplates={"application/json": response_template},
            )

        raw_body = "#set($result = $input.body)$result"
        body_in_str = "Action=SendMessage&MessageBody=$input.body"
        input_body_attr_access = "#set($result = $input.body.testAccess)$result"
        url_encode_body = "EncodedBody=$util.urlEncode($input.body)&EncodedBodyAccess=$util.urlEncode($input.body.testAccess)"
        _create_route(
            "raw-body",
            request_template=raw_body,
            response_template=raw_body,
        )
        _create_route(
            "str-body-input",
            request_template=body_in_str,
            response_template=raw_body,
        )
        _create_route(
            "str-body-output",
            request_template=raw_body,
            response_template=body_in_str,
        )
        _create_route(
            "str-body-all",
            request_template=body_in_str,
            response_template=body_in_str,
        )
        _create_route(
            "body-attr-access",
            request_template=input_body_attr_access,
            response_template=raw_body,
        )
        _create_route(
            "url-encode",
            request_template=url_encode_body,
            response_template=raw_body,
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        url = api_invoke_url(api_id=api_id, stage=stage_name, path="/")

        route_types = [
            "raw-body",
            "str-body-input",
            "str-body-output",
            "str-body-all",
            "body-attr-access",
            "url-encode",
        ]
        for route_type in route_types:
            route_url = url + route_type
            # we are using `response.content` on purpose in snapshot to have text response prefixed with `b''` to avoid
            # auto decoding of the possible JSON responses
            # TODO: remove headers parameter, this is due to issue in our Lambda URL parity, it B64 encodes data when
            #  AWS does not

            empty_body_response = requests.post(
                route_url, headers={"Content-Type": "application/json"}
            )
            json_body_response = requests.post(route_url, json={"some": "value"})
            str_body_response = requests.post(
                route_url, data=b"some raw data", headers={"Content-Type": "application/json"}
            )

            # keep the snapshot in one object to group related tests together
            snapshot.match(
                f"response-{route_type}",
                {
                    "empty-body": empty_body_response.content,
                    "json-body": json_body_response.content,
                    "str-body": str_body_response.content,
                },
            )


class TestUsagePlans:
    @markers.aws.validated
    def test_api_key_required_for_methods(
        self,
        aws_client,
        snapshot,
        create_rest_apigw,
        apigw_redeploy_api,
        cleanups,
    ):
        snapshot.add_transformer(snapshot.transform.apigateway_api())
        snapshot.add_transformers_list(
            [
                snapshot.transform.key_value("apiId"),
                snapshot.transform.key_value("value"),
            ]
        )

        # Create a REST API with the apiKeySource set to "HEADER"
        api_id, _, root_id = create_rest_apigw(name="test API key", apiKeySource="HEADER")

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="test"
        )

        resource_id = resource["id"]

        aws_client.apigateway.put_method(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="GET",
            authorizationType="NONE",
            apiKeyRequired=True,
        )

        aws_client.apigateway.put_method_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="GET",
            statusCode="200",
        )

        aws_client.apigateway.put_integration(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="GET",
            integrationHttpMethod="GET",
            type="MOCK",
            requestTemplates={"application/json": '{"statusCode": 200}'},
        )

        aws_client.apigateway.put_integration_response(
            restApiId=api_id,
            resourceId=resource_id,
            httpMethod="GET",
            statusCode="200",
            selectionPattern="",
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        usage_plan_response = aws_client.apigateway.create_usage_plan(
            name=f"test-plan-{short_uid()}",
            description="Test Usage Plan for API key",
            quota={"limit": 10, "period": "DAY", "offset": 0},
            throttle={"rateLimit": 2, "burstLimit": 1},
            apiStages=[{"apiId": api_id, "stage": stage_name}],
            tags={"tag_key": "tag_value"},
        )
        snapshot.match("create-usage-plan", usage_plan_response)

        usage_plan_id = usage_plan_response["id"]

        key_name = f"testApiKey-{short_uid()}"
        api_key_response = aws_client.apigateway.create_api_key(
            name=key_name,
            enabled=True,
        )
        snapshot.match("create-api-key", api_key_response)
        api_key_id = api_key_response["id"]
        cleanups.append(lambda: aws_client.apigateway.delete_api_key(apiKey=api_key_id))

        create_usage_plan_key_resp = aws_client.apigateway.create_usage_plan_key(
            usagePlanId=usage_plan_id,
            keyId=api_key_id,
            keyType="API_KEY",
        )
        snapshot.match("create-usage-plan-key", create_usage_plan_key_resp)

        url = api_invoke_url(api_id=api_id, stage=stage_name, path="/test")
        response = requests.get(url)
        # when the api key is not passed as part of the header
        assert response.status_code == 403

        def _assert_with_key(expected_status_code: int):
            _response = requests.get(url, headers={"x-api-key": api_key_response["value"]})
            assert _response.status_code == expected_status_code

        # AWS takes a very, very long time to make the key enabled
        retries = 10 if is_aws_cloud() else 3
        sleep = 12 if is_aws_cloud() else 1
        retry(_assert_with_key, retries=retries, sleep=sleep, expected_status_code=200)

        # now disable the key to verify that we should not be able to access the api
        patch_operations = [
            {"op": "replace", "path": "/enabled", "value": "false"},
        ]
        response = aws_client.apigateway.update_api_key(
            apiKey=api_key_id, patchOperations=patch_operations
        )
        snapshot.match("update-api-key-disabled", response)

        retry(_assert_with_key, retries=retries, sleep=sleep, expected_status_code=403)

    @markers.aws.validated
    def test_usage_plan_crud(self, create_rest_apigw, snapshot, aws_client, echo_http_server_post):
        snapshot.add_transformer(snapshot.transform.key_value("id", reference_replacement=True))
        snapshot.add_transformer(snapshot.transform.key_value("name"))
        snapshot.add_transformer(snapshot.transform.key_value("description"))
        snapshot.add_transformer(snapshot.transform.key_value("apiId", reference_replacement=True))

        # clean up any existing usage plans
        old_usage_plans = aws_client.apigateway.get_usage_plans().get("items", [])
        for usage_plan in old_usage_plans:
            aws_client.apigateway.delete_usage_plan(usagePlanId=usage_plan["id"])

        api_id, _, root = create_rest_apigw(
            name=f"test-api-{short_uid()}",
            description="this is my api",
        )

        create_rest_resource_method(
            aws_client.apigateway,
            restApiId=api_id,
            resourceId=root,
            httpMethod="GET",
            authorizationType="none",
        )

        create_rest_api_integration(
            aws_client.apigateway,
            restApiId=api_id,
            resourceId=root,
            httpMethod="GET",
            integrationHttpMethod="POST",
            type="HTTP",
            uri=echo_http_server_post,
        )

        deployment_id, _ = create_rest_api_deployment(aws_client.apigateway, restApiId=api_id)
        stage = create_rest_api_stage(
            aws_client.apigateway, restApiId=api_id, stageName="dev", deploymentId=deployment_id
        )

        # create usage plan
        response = aws_client.apigateway.create_usage_plan(
            name=f"test-usage-plan-{short_uid()}",
            description="this is my usage plan",
            apiStages=[
                {"apiId": api_id, "stage": stage},
            ],
        )
        snapshot.match("create-usage-plan", response)
        usage_plan_id = response["id"]

        # get usage plan
        response = aws_client.apigateway.get_usage_plan(usagePlanId=usage_plan_id)
        snapshot.match("get-usage-plan", response)

        # get usage plans
        response = aws_client.apigateway.get_usage_plans()
        snapshot.match("get-usage-plans", response)

        # update usage plan
        response = aws_client.apigateway.update_usage_plan(
            usagePlanId=usage_plan_id,
            patchOperations=[
                {"op": "replace", "path": "/throttle/burstLimit", "value": "100"},
                {"op": "replace", "path": "/throttle/rateLimit", "value": "200"},
                {"op": "replace", "path": "/quota/period", "value": "MONTH"},
                {"op": "replace", "path": "/quota/limit", "value": "5000"},
            ],
        )
        snapshot.match("update-usage-plan", response)

        if is_aws_cloud():
            # avoid TooManyRequests
            time.sleep(10)

        with pytest.raises(ClientError) as e:
            aws_client.apigateway.update_usage_plan(
                usagePlanId=usage_plan_id + "1",  # wrong ID
                patchOperations=[
                    {"op": "replace", "path": "/throttle/burstLimit", "value": "100"},
                    {"op": "replace", "path": "/throttle/rateLimit", "value": "200"},
                ],
            )
        snapshot.match("update-wrong-id", e.value.response)

        if is_aws_cloud():
            # avoid TooManyRequests
            time.sleep(10)

        with pytest.raises(ClientError) as e:
            aws_client.apigateway.update_usage_plan(
                usagePlanId=usage_plan_id,
                patchOperations=[
                    {"op": "remove", "path": "/apiStages"},
                ],
            )
        snapshot.match("update-wrong-api-stages-no-value", e.value.response)

        if is_aws_cloud():
            # avoid TooManyRequests
            time.sleep(10)

        with pytest.raises(ClientError) as e:
            wrong_api_id = api_id + "b"
            aws_client.apigateway.update_usage_plan(
                usagePlanId=usage_plan_id,
                patchOperations=[
                    {"op": "remove", "path": "/apiStages", "value": f"{wrong_api_id}:{stage}"},
                ],
            )
        snapshot.match("update-wrong-api-stages-wrong-api", e.value.response)

        if is_aws_cloud():
            # avoid TooManyRequests
            time.sleep(10)

        with pytest.raises(ClientError) as e:
            aws_client.apigateway.update_usage_plan(
                usagePlanId=usage_plan_id,
                patchOperations=[
                    {"op": "remove", "path": "/apiStages", "value": f"{api_id}:fakestagename"},
                ],
            )
        snapshot.match("update-wrong-api-stages-wrong-stage", e.value.response)

        if is_aws_cloud():
            # avoid TooManyRequests
            time.sleep(10)

        with pytest.raises(ClientError) as e:
            aws_client.apigateway.update_usage_plan(
                usagePlanId=usage_plan_id,
                patchOperations=[
                    {"op": "remove", "path": "/apiStages", "value": "fakevalue"},
                ],
            )
        snapshot.match("update-wrong-api-stages-wrong-value", e.value.response)

        # get usage plan after update
        response = aws_client.apigateway.get_usage_plan(usagePlanId=usage_plan_id)
        snapshot.match("get-usage-plan-after-update", response)

        # get usage plans after update
        response = aws_client.apigateway.get_usage_plans()
        snapshot.match("get-usage-plans-after-update", response)


class TestDocumentations:
    @markers.aws.validated
    def test_documentation_parts_and_versions(
        self, aws_client, create_rest_apigw, apigw_add_transformers, snapshot
    ):
        client = aws_client.apigateway

        # create API
        api_id, api_name, root_id = create_rest_apigw()

        # create documentation part
        response = client.create_documentation_part(
            restApiId=api_id,
            location={"type": "API"},
            properties=json.dumps({"foo": "bar"}),
        )
        snapshot.match("create-part-response", response)

        response = client.get_documentation_parts(restApiId=api_id)
        snapshot.match("get-parts-response", response)

        # create/update/get documentation version

        response = client.create_documentation_version(
            restApiId=api_id, documentationVersion="v123"
        )
        snapshot.match("create-version-response", response)

        response = client.update_documentation_version(
            restApiId=api_id,
            documentationVersion="v123",
            patchOperations=[{"op": "replace", "path": "/description", "value": "doc version new"}],
        )
        snapshot.match("update-version-response", response)

        response = client.get_documentation_version(restApiId=api_id, documentationVersion="v123")
        snapshot.match("get-version-response", response)


class TestStages:
    @pytest.fixture
    def _create_api_with_stage(
        self, aws_client, create_rest_apigw, apigw_add_transformers, snapshot
    ):
        client = aws_client.apigateway

        def _create():
            # create API, method, integration, deployment
            api_id, api_name, root_id = create_rest_apigw()
            client.put_method(
                restApiId=api_id, resourceId=root_id, httpMethod="GET", authorizationType="NONE"
            )
            client.put_integration(
                restApiId=api_id, resourceId=root_id, httpMethod="GET", type="MOCK"
            )
            response = client.create_deployment(restApiId=api_id)
            deployment_id = response["id"]

            # create documentation
            client.create_documentation_part(
                restApiId=api_id,
                location={"type": "API"},
                properties=json.dumps({"foo": "bar"}),
            )
            client.create_documentation_version(restApiId=api_id, documentationVersion="v123")

            # create stage
            response = client.create_stage(
                restApiId=api_id,
                stageName="s1",
                deploymentId=deployment_id,
                description="my stage",
                documentationVersion="v123",
            )
            snapshot.match("create-stage", response)

            return api_id

        return _create

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(paths=["$..createdDate", "$..lastUpdatedDate"])
    def test_create_update_stages(
        self, _create_api_with_stage, aws_client, create_rest_apigw, snapshot
    ):
        client = aws_client.apigateway
        api_id = _create_api_with_stage()

        # negative tests for immutable/non-updateable attributes

        with pytest.raises(ClientError) as ctx:
            client.update_stage(
                restApiId=api_id,
                stageName="s1",
                patchOperations=[
                    {"op": "replace", "path": "/documentation_version", "value": "123"}
                ],
            )
        snapshot.match("error-update-doc-version", ctx.value.response)

        with pytest.raises(ClientError) as ctx:
            client.update_stage(
                restApiId=api_id,
                stageName="s1",
                patchOperations=[
                    {"op": "replace", "path": "/tags/tag1", "value": "value1"},
                ],
            )
        snapshot.match("error-update-tags", ctx.value.response)

        # update & get stage
        response = client.update_stage(
            restApiId=api_id,
            stageName="s1",
            patchOperations=[
                {"op": "replace", "path": "/description", "value": "stage new"},
                {"op": "replace", "path": "/variables/var1", "value": "test"},
                {"op": "replace", "path": "/variables/var2", "value": "test2"},
                {"op": "replace", "path": "/*/*/throttling/burstLimit", "value": "123"},
                {"op": "replace", "path": "/*/*/caching/enabled", "value": "true"},
                {"op": "replace", "path": "/tracingEnabled", "value": "true"},
                {"op": "replace", "path": "/test/GET/throttling/burstLimit", "value": "124"},
            ],
        )
        snapshot.match("update-stage", response)

        response = client.get_stage(restApiId=api_id, stageName="s1")
        snapshot.match("get-stage", response)

        # show that updating */* does not override previously set values, only
        # provides default values then like shown above
        response = client.update_stage(
            restApiId=api_id,
            stageName="s1",
            patchOperations=[
                {"op": "replace", "path": "/*/*/throttling/burstLimit", "value": "100"},
            ],
        )
        snapshot.match("update-stage-override", response)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(paths=["$..createdDate", "$..lastUpdatedDate"])
    def test_update_stage_remove_wildcard(self, aws_client, _create_api_with_stage, snapshot):
        client = aws_client.apigateway
        api_id = _create_api_with_stage()

        response = client.get_stage(restApiId=api_id, stageName="s1")
        snapshot.match("get-stage", response)

        def _delete_wildcard():
            # remove all attributes at path */* (this is an operation Terraform executes when deleting APIs)
            response = client.update_stage(
                restApiId=api_id,
                stageName="s1",
                patchOperations=[
                    {"op": "remove", "path": "/*/*"},
                ],
            )
            snapshot.match("update-stage-reset", response)

        # attempt to delete wildcard method settings (should initially fail)
        with pytest.raises(ClientError) as exc:
            _delete_wildcard()
        snapshot.match("delete-error", exc.value.response)

        # run a patch operation that creates a method mapping for */*
        response = client.update_stage(
            restApiId=api_id,
            stageName="s1",
            patchOperations=[
                {"op": "replace", "path": "/*/*/caching/enabled", "value": "true"},
            ],
        )
        snapshot.match("update-stage", response)

        # delete wildcard method settings (should now succeed)
        _delete_wildcard()

        # assert the content of the stage after the update
        response = client.get_stage(restApiId=api_id, stageName="s1")
        snapshot.match("get-stage-after-reset", response)


class TestDeployments:
    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(paths=["$..createdDate", "$..lastUpdatedDate"])
    @pytest.mark.parametrize("create_stage_manually", [True, False])
    def test_create_delete_deployments(
        self, create_stage_manually, aws_client, create_rest_apigw, apigw_add_transformers, snapshot
    ):
        snapshot.add_transformer(snapshot.transform.apigateway_api())
        client = aws_client.apigateway

        # create API, method, integration, deployment
        api_id, _, root_id = create_rest_apigw()
        client.put_method(
            restApiId=api_id, resourceId=root_id, httpMethod="GET", authorizationType="NONE"
        )
        client.put_integration(restApiId=api_id, resourceId=root_id, httpMethod="GET", type="MOCK")

        # create deployment - stage can be passed as parameter, or created separately below
        kwargs = {} if create_stage_manually else {"stageName": "s1"}
        response = client.create_deployment(restApiId=api_id, **kwargs)
        deployment_id = response["id"]

        # create stage
        if create_stage_manually:
            client.create_stage(restApiId=api_id, stageName="s1", deploymentId=deployment_id)

        # get deployment and stages
        response = client.get_deployment(restApiId=api_id, deploymentId=deployment_id)
        snapshot.match("get-deployment", response)
        response = client.get_stages(restApiId=api_id)
        snapshot.match("get-stages", response)

        for i in range(3):
            # asset that deleting the deployment fails if stage exists
            with pytest.raises(ClientError) as ctx:
                client.delete_deployment(restApiId=api_id, deploymentId=deployment_id)
            snapshot.match(f"delete-deployment-error-{i}", ctx.value.response)

            # delete stage and deployment
            client.delete_stage(restApiId=api_id, stageName="s1")
            client.delete_deployment(restApiId=api_id, deploymentId=deployment_id)

            # re-create stage and deployment
            response = client.create_deployment(restApiId=api_id, **kwargs)
            deployment_id = response["id"]
            if create_stage_manually:
                client.create_stage(restApiId=api_id, stageName="s1", deploymentId=deployment_id)

            # list deployments and stages again
            response = client.get_deployments(restApiId=api_id)
            snapshot.match(f"get-deployments-{i}", response)
            response = client.get_stages(restApiId=api_id)
            snapshot.match(f"get-stages-{i}", response)

    @markers.aws.validated
    @markers.snapshot.skip_snapshot_verify(paths=["$..createdDate", "$..lastUpdatedDate"])
    def test_create_update_deployments(
        self, aws_client, create_rest_apigw, apigw_add_transformers, snapshot
    ):
        snapshot.add_transformer(snapshot.transform.apigateway_api())
        client = aws_client.apigateway

        # create API, method, integration, deployment
        api_id, _, root_id = create_rest_apigw()
        client.put_method(
            restApiId=api_id, resourceId=root_id, httpMethod="GET", authorizationType="NONE"
        )
        client.put_integration(restApiId=api_id, resourceId=root_id, httpMethod="GET", type="MOCK")

        # create deployment - stage can be passed as parameter, or created separately below
        response = client.create_deployment(restApiId=api_id)
        deployment_id_1 = response["id"]

        # create stage
        client.create_stage(restApiId=api_id, stageName="s1", deploymentId=deployment_id_1)

        # get deployment and stages
        response = client.get_deployment(restApiId=api_id, deploymentId=deployment_id_1)
        snapshot.match("get-deployment-1", response)
        response = client.get_stages(restApiId=api_id)
        snapshot.match("get-stages", response)

        # asset that deleting the deployment fails if stage exists
        with pytest.raises(ClientError) as ctx:
            client.delete_deployment(restApiId=api_id, deploymentId=deployment_id_1)
        snapshot.match("delete-deployment-error", ctx.value.response)

        # create another deployment with the previous stage, which should update the stage
        response = client.create_deployment(restApiId=api_id, stageName="s1")
        deployment_id_2 = response["id"]

        # get deployments and stages
        response = client.get_deployment(restApiId=api_id, deploymentId=deployment_id_1)
        snapshot.match("get-deployment-1-after-update", response)
        response = client.get_deployment(restApiId=api_id, deploymentId=deployment_id_2)
        snapshot.match("get-deployment-2", response)
        response = client.get_stages(restApiId=api_id)
        snapshot.match("get-stages-after-update", response)

        response = client.delete_deployment(restApiId=api_id, deploymentId=deployment_id_1)
        snapshot.match("delete-deployment-1", response)

        # asset that deleting the deployment fails if stage exists
        with pytest.raises(ClientError) as ctx:
            client.delete_deployment(restApiId=api_id, deploymentId=deployment_id_2)
        snapshot.match("delete-deployment-2-error", ctx.value.response)


class TestApigatewayRouting:
    @markers.aws.validated
    def test_proxy_routing_with_hardcoded_resource_sibling(self, aws_client, create_rest_apigw):
        api_id, _, root_id = create_rest_apigw(name="test proxy routing")

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="test"
        )
        hardcoded_resource_id = resource["id"]

        response_template_post = {"statusCode": 200, "message": "POST request"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, hardcoded_resource_id, "POST", response_template_post
        )

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=hardcoded_resource_id, pathPart="any"
        )
        any_resource_id = resource["id"]

        response_template_any = {"statusCode": 200, "message": "ANY request"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, any_resource_id, "ANY", response_template_any
        )

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="{proxy+}"
        )
        proxy_resource_id = resource["id"]
        response_template_options = {"statusCode": 200, "message": "OPTIONS request"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, proxy_resource_id, "OPTIONS", response_template_options
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        url = api_invoke_url(api_id=api_id, stage=stage_name, path="/test")

        def _invoke_api(req_url: str, http_method: str, expected_type: str):
            _response = requests.request(http_method.upper(), req_url)
            assert _response.ok
            assert _response.json()["message"] == f"{expected_type} request"

        retries = 10 if is_aws_cloud() else 3
        sleep = 3 if is_aws_cloud() else 1
        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            req_url=url,
            http_method="OPTIONS",
            expected_type="OPTIONS",
        )
        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            req_url=url,
            http_method="POST",
            expected_type="POST",
        )
        any_url = api_invoke_url(api_id=api_id, stage=stage_name, path="/test/any")
        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            req_url=any_url,
            http_method="OPTIONS",
            expected_type="ANY",
        )
        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            req_url=any_url,
            http_method="GET",
            expected_type="ANY",
        )

    @markers.aws.validated
    def test_routing_with_hardcoded_resource_sibling_order(self, aws_client, create_rest_apigw):
        api_id, _, root_id = create_rest_apigw(name="test parameter routing")

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="part1"
        )
        hardcoded_resource_id = resource["id"]

        response_template_get = {"statusCode": 200, "message": "part1"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, hardcoded_resource_id, "GET", response_template_get
        )

        # define the proxy before so that it would come up as the first resource iterated over
        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="{param+}"
        )
        proxy_resource_id = resource["id"]
        response_template_get = {"statusCode": 200, "message": "proxy"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, proxy_resource_id, "GET", response_template_get
        )

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=hardcoded_resource_id, pathPart="hardcoded-value"
        )
        any_resource_id = resource["id"]

        response_template_get = {"statusCode": 200, "message": "hardcoded-value"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, any_resource_id, "GET", response_template_get
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        def _invoke_api(path: str, expected_response: str):
            url = api_invoke_url(api_id=api_id, stage=stage_name, path=path)
            _response = requests.get(url)
            assert _response.ok
            assert _response.json()["message"] == expected_response

        retries = 10 if is_aws_cloud() else 3
        sleep = 3 if is_aws_cloud() else 1
        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            path="/part1",
            expected_response="part1",
        )
        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            path="/part1/hardcoded-value",
            expected_response="hardcoded-value",
        )

        retry(
            _invoke_api,
            retries=retries,
            sleep=sleep,
            path="/part1/random-value",
            expected_response="proxy",
        )

    @markers.aws.validated
    @pytest.mark.skipif(
        condition=not is_next_gen_api() and not is_aws_cloud(),
        reason="Wrong behavior in legacy implementation",
    )
    def test_routing_not_found(self, aws_client, create_rest_apigw, snapshot):
        api_id, _, root_id = create_rest_apigw(name=f"test-notfound-{short_uid()}")

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="existing"
        )
        hardcoded_resource_id = resource["id"]

        response_template_get = {"statusCode": 200, "message": "exists"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, hardcoded_resource_id, "GET", response_template_get
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        def _invoke_api(path: str, method: str, works: bool):
            url = api_invoke_url(api_id=api_id, stage=stage_name, path=path)
            _response = requests.request(method, url)
            assert _response.ok == works
            return _response

        retry_args = {"retries": 10 if is_aws_cloud() else 3, "sleep": 3 if is_aws_cloud() else 1}
        response = retry(_invoke_api, method="GET", path="/existing", works=True, **retry_args)
        snapshot.match("working-route", response.json())

        response = retry(
            _invoke_api, method="GET", path="/random-non-existing", works=False, **retry_args
        )
        resp = {
            "content": response.json(),
            "errorType": response.headers.get("x-amzn-ErrorType"),
        }
        snapshot.match("not-found", resp)

        response = retry(_invoke_api, method="POST", path="/existing", works=False, **retry_args)
        resp = {
            "content": response.json(),
            "errorType": response.headers.get("x-amzn-ErrorType"),
        }
        snapshot.match("wrong-method", resp)

    @markers.aws.only_localstack
    def test_api_not_existing(self, aws_client, create_rest_apigw, snapshot):
        """
        This cannot be tested against AWS, as this is the format: `https://<api-id>.execute-api.<region>.amazonaws.com`
        So if the API does not exist, the DNS subdomain is not created and is not reachable.
        This test document the expected behavior for LocalStack.
        """
        aws_client.apigateway.get_rest_apis()
        endpoint_url = api_invoke_url(api_id="404api", stage="dev", path="/test-path")

        _response = requests.get(endpoint_url)

        assert _response.status_code == 404
        if not is_next_gen_api():
            assert not _response.content

        else:
            assert _response.json() == {
                "message": "The API id '404api' does not correspond to a deployed API Gateway API"
            }

    @markers.aws.only_localstack
    def test_routing_with_custom_api_id(self, aws_client, create_rest_apigw):
        custom_id = "custom-api-id"
        api_id, _, root_id = create_rest_apigw(
            name="test custom id routing", tags={TAG_KEY_CUSTOM_ID: custom_id}
        )

        resource = aws_client.apigateway.create_resource(
            restApiId=api_id, parentId=root_id, pathPart="part1"
        )
        hardcoded_resource_id = resource["id"]

        response_template_get = {"statusCode": 200, "message": "routing ok"}
        _create_mock_integration_with_200_response_template(
            aws_client, api_id, hardcoded_resource_id, "GET", response_template_get
        )

        stage_name = "dev"
        aws_client.apigateway.create_deployment(restApiId=api_id, stageName=stage_name)

        url = api_invoke_url(api_id=api_id, stage=stage_name, path="/part1")
        response = requests.get(url)
        assert response.ok
        assert response.json()["message"] == "routing ok"

        # Validated test living here: `test_create_execute_api_vpc_endpoint`
        vpce_url = url.replace(custom_id, f"{custom_id}-vpce-aabbaabbaabbaabba")
        response = requests.get(vpce_url)
        assert response.ok
        assert response.json()["message"] == "routing ok"
