#!/usr/bin/env node
import 'source-map-support/register';
import * as cdk from 'aws-cdk-lib';
import { OidcStack } from '../lib/oidc-stack';
import { BedrockApiStack } from '../lib/bedrock-api-stack';

const app = new cdk.App();

const account = process.env.CDK_DEFAULT_ACCOUNT;
const region = app.node.tryGetContext('region') ?? process.env.CDK_DEFAULT_REGION ?? 'ap-southeast-2';
// When deploying for real, account+region resolve from the AWS profile and
// give us env-aware features. When validating with `cdk synth` in CI without
// AWS credentials, we leave env undefined and CDK emits region-agnostic CFN.
const env = account ? { account, region } : undefined;

const githubOrg = app.node.tryGetContext('githubOrg');
const githubRepo = app.node.tryGetContext('githubRepo');
if (!githubOrg || !githubRepo) {
  throw new Error('Pass --context githubOrg=<org> --context githubRepo=<repo> when synthesising/deploying.');
}

const modelId = app.node.tryGetContext('modelId') ?? 'au.anthropic.claude-opus-4-6-v1';

new OidcStack(app, 'BedrockApiOidc', {
  env,
  githubOrg,
  githubRepo,
  ecrRepoName: 'bedrock-api',
});

new BedrockApiStack(app, 'BedrockApi', {
  env,
  modelId,
  ecrRepoName: 'bedrock-api',
});
