#!/usr/bin/env python3
import os

import aws_cdk as cdk

from voice_agent_stack import VoiceAgentStack


app = cdk.App()

env_name = app.node.try_get_context("env") or "dev"
aws_account = app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT")
aws_region = app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION") or "us-east-1"

VoiceAgentStack(
    app,
    f"VoiceAgent-{env_name}",
    env_name=env_name,
    env=cdk.Environment(account=aws_account, region=aws_region),
)

app.synth()
