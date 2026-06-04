from pathlib import Path

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_certificatemanager as acm,
    aws_cloudwatch as cloudwatch,
    aws_dynamodb as dynamodb,
    aws_ec2 as ec2,
    aws_ecr_assets as ecr_assets,
    aws_ecs as ecs,
    aws_elasticloadbalancingv2 as elbv2,
    aws_iam as iam,
    aws_logs as logs,
    aws_secretsmanager as secretsmanager,
    aws_ssm as ssm,
)
from constructs import Construct


class VoiceAgentStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, env_name: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.env_name = _required_context_value(env_name, "env")
        default_persona_id = self._context("defaultPersonaId", "warm_clinical_followup")
        bedrock_region = self._context("bedrockRegion", self.region)
        nova_model_id = self._context("novaModelId", "amazon.nova-2-sonic-v1:0")
        domain_name = self.node.try_get_context("domainName")
        certificate_arn = self.node.try_get_context("certificateArn")
        twilio_auth_token_secret_arn = self.node.try_get_context("twilioAuthTokenSecretArn")

        resource_prefix = f"voice-agent-{self.env_name}"
        sessions_table = self._session_table(resource_prefix)
        personas_table = self._persona_table(resource_prefix)
        transcript_turns_table = self._transcript_table(resource_prefix)

        vpc = ec2.Vpc(
            self,
            "Vpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        cluster = ecs.Cluster(self, "Cluster", vpc=vpc, cluster_name=f"{resource_prefix}-cluster")

        log_group = logs.LogGroup(
            self,
            "ServiceLogGroup",
            log_group_name=f"/aws/ecs/{resource_prefix}",
            retention=logs.RetentionDays.TWO_WEEKS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        task_definition = ecs.FargateTaskDefinition(
            self,
            "TaskDefinition",
            cpu=512,
            memory_limit_mib=1024,
            family=f"{resource_prefix}-task",
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64,
                operating_system_family=ecs.OperatingSystemFamily.LINUX,
            ),
        )

        repository_root = Path(__file__).resolve().parents[1]
        container = task_definition.add_container(
            "AppContainer",
            image=ecs.ContainerImage.from_asset(
                str(repository_root),
                platform=ecr_assets.Platform.LINUX_AMD64,
            ),
            logging=ecs.LogDrivers.aws_logs(log_group=log_group, stream_prefix="app"),
            environment={
                "ENV_NAME": self.env_name,
                "DEFAULT_PERSONA_ID": default_persona_id,
                "VERIFY_TWILIO_SIGNATURE": "true",
                "SESSIONS_TABLE_NAME": sessions_table.table_name,
                "PERSONAS_TABLE_NAME": personas_table.table_name,
                "TRANSCRIPT_TURNS_TABLE_NAME": transcript_turns_table.table_name,
                "BEDROCK_REGION": bedrock_region,
                "NOVA_MODEL_ID": nova_model_id,
            },
        )
        container.add_port_mappings(ecs.PortMapping(container_port=8080, protocol=ecs.Protocol.TCP))

        if twilio_auth_token_secret_arn:
            twilio_secret = secretsmanager.Secret.from_secret_complete_arn(
                self,
                "TwilioAuthTokenSecret",
                str(twilio_auth_token_secret_arn),
            )
            container.add_secret("TWILIO_AUTH_TOKEN", ecs.Secret.from_secrets_manager(twilio_secret))
            twilio_secret.grant_read(task_definition.task_role)
            twilio_secret.grant_read(task_definition.execution_role)

        self._grant_runtime_permissions(
            task_definition=task_definition,
            tables=[sessions_table, personas_table, transcript_turns_table],
            bedrock_region=bedrock_region,
            nova_model_id=nova_model_id,
        )

        alb_security_group = ec2.SecurityGroup(self, "AlbSecurityGroup", vpc=vpc)
        service_security_group = ec2.SecurityGroup(self, "ServiceSecurityGroup", vpc=vpc)
        service_security_group.connections.allow_from(
            alb_security_group,
            ec2.Port.tcp(8080),
            "Allow ALB traffic to FastAPI",
        )

        load_balancer = elbv2.ApplicationLoadBalancer(
            self,
            "LoadBalancer",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_security_group,
            load_balancer_name=f"{resource_prefix}-alb",
        )

        listener = self._listener(
            load_balancer=load_balancer,
            certificate_arn=certificate_arn,
        )

        service = ecs.FargateService(
            self,
            "Service",
            cluster=cluster,
            task_definition=task_definition,
            desired_count=1,
            assign_public_ip=True,
            security_groups=[service_security_group],
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
            service_name=f"{resource_prefix}-service",
            health_check_grace_period=Duration.seconds(60),
            min_healthy_percent=100,
        )

        listener.add_targets(
            "ServiceTarget",
            port=8080,
            protocol=elbv2.ApplicationProtocol.HTTP,
            targets=[service],
            health_check=elbv2.HealthCheck(
                path="/health",
                healthy_http_codes="200",
                interval=Duration.seconds(30),
                timeout=Duration.seconds(5),
            ),
        )

        public_base_url = self._public_base_url(load_balancer=load_balancer, domain_name=domain_name, use_tls=bool(certificate_arn))
        container.add_environment("PUBLIC_BASE_URL", public_base_url)

        self._create_ssm_config_parameters(
            resource_prefix=resource_prefix,
            public_base_url=public_base_url,
            default_persona_id=default_persona_id,
            bedrock_region=bedrock_region,
            table_names={
                "sessions": sessions_table.table_name,
                "personas": personas_table.table_name,
                "transcript-turns": transcript_turns_table.table_name,
            },
        )

        cloudwatch.Alarm(
            self,
            "ErrorCountAlarm",
            alarm_name=f"{resource_prefix}-error-count",
            metric=cloudwatch.Metric(
                namespace="AWSTwilioVoiceAssistant",
                metric_name="ErrorCount",
                statistic="Sum",
                period=Duration.minutes(5),
            ),
            threshold=1,
            evaluation_periods=1,
            datapoints_to_alarm=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

        cdk.CfnOutput(self, "LoadBalancerDnsName", value=load_balancer.load_balancer_dns_name)
        cdk.CfnOutput(self, "PublicBaseUrl", value=public_base_url)
        cdk.CfnOutput(self, "TwilioVoiceWebhookUrl", value=f"{public_base_url}/twilio/voice")
        cdk.CfnOutput(self, "TwilioMediaWebSocketUrl", value=f"{_websocket_scheme(public_base_url)}/media")

    def _session_table(self, resource_prefix: str) -> dynamodb.Table:
        return dynamodb.Table(
            self,
            "SessionsTable",
            table_name=f"{resource_prefix}-sessions",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _persona_table(self, resource_prefix: str) -> dynamodb.Table:
        return dynamodb.Table(
            self,
            "PersonasTable",
            table_name=f"{resource_prefix}-personas",
            partition_key=dynamodb.Attribute(name="persona_id", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _transcript_table(self, resource_prefix: str) -> dynamodb.Table:
        return dynamodb.Table(
            self,
            "TranscriptTurnsTable",
            table_name=f"{resource_prefix}-transcript-turns",
            partition_key=dynamodb.Attribute(name="session_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="turn_index", type=dynamodb.AttributeType.NUMBER),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
        )

    def _listener(
        self,
        *,
        load_balancer: elbv2.ApplicationLoadBalancer,
        certificate_arn: object,
    ) -> elbv2.ApplicationListener:
        if certificate_arn:
            certificate = acm.Certificate.from_certificate_arn(self, "AlbCertificate", str(certificate_arn))
            http_listener = load_balancer.add_listener(
                "HttpListener",
                port=80,
                protocol=elbv2.ApplicationProtocol.HTTP,
                open=True,
                default_action=elbv2.ListenerAction.redirect(protocol="HTTPS", port="443", permanent=True),
            )
            return load_balancer.add_listener(
                "HttpsListener",
                port=443,
                protocol=elbv2.ApplicationProtocol.HTTPS,
                certificates=[certificate],
                open=True,
            )

        return load_balancer.add_listener(
            "HttpListener",
            port=80,
            protocol=elbv2.ApplicationProtocol.HTTP,
            open=True,
        )

    def _grant_runtime_permissions(
        self,
        *,
        task_definition: ecs.FargateTaskDefinition,
        tables: list[dynamodb.Table],
        bedrock_region: str,
        nova_model_id: str,
    ) -> None:
        for table in tables:
            table.grant_read_write_data(task_definition.task_role)

        task_definition.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["bedrock:InvokeModel"],
                resources=[
                    f"arn:aws:bedrock:{bedrock_region}::foundation-model/{nova_model_id}",
                    f"arn:aws:bedrock:{bedrock_region}:{cdk.Aws.ACCOUNT_ID}:inference-profile/*",
                ],
            )
        )

        task_definition.task_role.add_to_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"],
                resources=[f"arn:aws:ssm:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:parameter/voice-agent/{self.env_name}/*"],
            )
        )

    def _create_ssm_config_parameters(
        self,
        *,
        resource_prefix: str,
        public_base_url: str,
        default_persona_id: str,
        bedrock_region: str,
        table_names: dict[str, str],
    ) -> None:
        base_path = f"/voice-agent/{self.env_name}"
        values = {
            "public-base-url": public_base_url,
            "default-persona-id": default_persona_id,
            "bedrock-region": bedrock_region,
            **{f"{name}-table-name": table_name for name, table_name in table_names.items()},
        }
        for name, value in values.items():
            ssm.StringParameter(
                self,
                f"Ssm{name.title().replace('-', '')}",
                parameter_name=f"{base_path}/{name}",
                string_value=value,
                description=f"{resource_prefix} deployed config: {name}",
            )

    def _public_base_url(
        self,
        *,
        load_balancer: elbv2.ApplicationLoadBalancer,
        domain_name: object,
        use_tls: bool,
    ) -> str:
        if domain_name:
            return f"https://{str(domain_name).strip('/')}"
        scheme = "https" if use_tls else "http"
        return f"{scheme}://{load_balancer.load_balancer_dns_name}"

    def _context(self, name: str, default: str) -> str:
        value = self.node.try_get_context(name)
        if value is None:
            return default
        return _required_context_value(str(value), name)


def _required_context_value(value: str, name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"CDK context value {name} cannot be empty")
    return stripped


def _websocket_scheme(public_base_url: str) -> str:
    if public_base_url.startswith("https://"):
        return "wss://" + public_base_url.removeprefix("https://").rstrip("/")
    return "ws://" + public_base_url.removeprefix("http://").rstrip("/")
