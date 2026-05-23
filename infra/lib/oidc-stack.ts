import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import { Construct } from 'constructs';

export interface OidcStackProps extends cdk.StackProps {
  /** GitHub org/user that owns the repo */
  githubOrg: string;
  /** GitHub repo name */
  githubRepo: string;
  /** ECR repo name the role is allowed to push to */
  ecrRepoName: string;
}

/**
 * One-time bootstrap stack. Creates:
 *   - the ECR repository (so images can be pushed before the app stack exists)
 *   - the GitHub OIDC provider
 *   - the deploy role assumed by GitHub Actions via OIDC
 *
 * Deploy with: cdk deploy BedrockApiOidc --context githubOrg=... --context githubRepo=...
 */
export class OidcStack extends cdk.Stack {
  public readonly deployRole: iam.Role;
  public readonly repo: ecr.Repository;

  constructor(scope: Construct, id: string, props: OidcStackProps) {
    super(scope, id, props);

    // ---- ECR repo (created here so we can push images before the app stack)
    this.repo = new ecr.Repository(this, 'Repo', {
      repositoryName: props.ecrRepoName,
      imageScanOnPush: true,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
      lifecycleRules: [{ maxImageCount: 10, description: 'Keep last 10 images' }],
    });

    // ---- GitHub OIDC provider + deploy role
    const provider = new iam.OpenIdConnectProvider(this, 'GithubOidcProvider', {
      url: 'https://token.actions.githubusercontent.com',
      clientIds: ['sts.amazonaws.com'],
    });

    const principal = new iam.FederatedPrincipal(
      provider.openIdConnectProviderArn,
      {
        StringEquals: {
          'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
        },
        StringLike: {
          'token.actions.githubusercontent.com:sub': `repo:${props.githubOrg}/${props.githubRepo}:*`,
        },
      },
      'sts:AssumeRoleWithWebIdentity',
    );

    this.deployRole = new iam.Role(this, 'GithubActionsDeployRole', {
      roleName: 'BedrockApiGithubDeployRole',
      assumedBy: principal,
      description: `Assumed by GitHub Actions for ${props.githubOrg}/${props.githubRepo}`,
      maxSessionDuration: cdk.Duration.hours(1),
    });

    // ECR push permissions (scoped to the named repo)
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['ecr:GetAuthorizationToken'],
      resources: ['*'],
    }));
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecr:BatchCheckLayerAvailability',
        'ecr:CompleteLayerUpload',
        'ecr:InitiateLayerUpload',
        'ecr:PutImage',
        'ecr:UploadLayerPart',
        'ecr:DescribeRepositories',
        'ecr:DescribeImages',
        'ecr:BatchGetImage',
      ],
      resources: [
        `arn:aws:ecr:${this.region}:${this.account}:repository/${props.ecrRepoName}`,
      ],
    }));

    // Allow assuming the CDK bootstrap roles so `cdk deploy` works.
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['sts:AssumeRole'],
      resources: [
        `arn:aws:iam::${this.account}:role/cdk-*-deploy-role-*`,
        `arn:aws:iam::${this.account}:role/cdk-*-file-publishing-role-*`,
        `arn:aws:iam::${this.account}:role/cdk-*-image-publishing-role-*`,
        `arn:aws:iam::${this.account}:role/cdk-*-lookup-role-*`,
      ],
    }));

    // Post-deploy: read CFN outputs (cluster + service names) and force a new
    // ECS deployment so tasks pull the freshly pushed :latest image.
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: ['cloudformation:DescribeStacks'],
      resources: [
        `arn:aws:cloudformation:${this.region}:${this.account}:stack/BedrockApi/*`,
      ],
    }));
    this.deployRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'ecs:UpdateService',
        'ecs:DescribeServices',
      ],
      // Service ARN isn't known until BedrockApi is deployed; scope by region+account.
      resources: [
        `arn:aws:ecs:${this.region}:${this.account}:service/*/*`,
      ],
    }));

    new cdk.CfnOutput(this, 'DeployRoleArn', {
      value: this.deployRole.roleArn,
      description: 'Set this as the AWS_ROLE_TO_ASSUME secret in GitHub Actions.',
    });
    new cdk.CfnOutput(this, 'EcrRepoUri', {
      value: this.repo.repositoryUri,
      description: 'Push the application image here before deploying BedrockApi.',
    });
  }
}
