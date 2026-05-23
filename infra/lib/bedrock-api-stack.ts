import * as cdk from 'aws-cdk-lib';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as ecr from 'aws-cdk-lib/aws-ecr';
import * as ecs from 'aws-cdk-lib/aws-ecs';
import * as elbv2 from 'aws-cdk-lib/aws-elasticloadbalancingv2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as cloudfront from 'aws-cdk-lib/aws-cloudfront';
import * as origins from 'aws-cdk-lib/aws-cloudfront-origins';
import { Construct } from 'constructs';

export interface BedrockApiStackProps extends cdk.StackProps {
  /** Bedrock model id or inference profile id (e.g. au.anthropic.claude-opus-4-6-v1) */
  modelId: string;
  /** ECR repo name (must match OidcStack ecrRepoName) */
  ecrRepoName: string;
}

export class BedrockApiStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: BedrockApiStackProps) {
    super(scope, id, props);

    // ---- VPC: 2 AZ, public-only (no NAT) ---------------------------------
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { name: 'public', subnetType: ec2.SubnetType.PUBLIC, cidrMask: 24 },
      ],
    });

    // ---- ECR repo: imported (created in OidcStack) -----------------------
    const repo = ecr.Repository.fromRepositoryName(this, 'Repo', props.ecrRepoName);

    // ---- Secrets Manager: tenant API keys --------------------------------
    const tenantKeys = new secretsmanager.Secret(this, 'TenantKeys', {
      secretName: 'bedrock-api/tenant-keys',
      description: 'JSON map of {tenantId: apiKey} for the OpenAI-compatible API',
      secretStringValue: cdk.SecretValue.unsafePlainText('{}'),
    });

    // ---- ECS cluster, log group, task role -------------------------------
    const cluster = new ecs.Cluster(this, 'Cluster', { vpc });
    const logGroup = new logs.LogGroup(this, 'AppLogs', {
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const taskRole = new iam.Role(this, 'TaskRole', {
      assumedBy: new iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
      description: 'Runtime role for the Bedrock-fronted API task',
    });

    taskRole.addToPolicy(new iam.PolicyStatement({
      actions: [
        'bedrock:Converse',
        'bedrock:ConverseStream',
        'bedrock:InvokeModel',
        'bedrock:InvokeModelWithResponseStream',
      ],
      resources: [
        `arn:aws:bedrock:*::foundation-model/*`,
        `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/*`,
        `arn:aws:bedrock:*:${this.account}:application-inference-profile/*`,
      ],
    }));

    tenantKeys.grantRead(taskRole);

    // ---- Task definition (ARM64 / Graviton) ------------------------------
    const taskDef = new ecs.FargateTaskDefinition(this, 'TaskDef', {
      cpu: 512,
      memoryLimitMiB: 1024,
      runtimePlatform: {
        cpuArchitecture: ecs.CpuArchitecture.ARM64,
        operatingSystemFamily: ecs.OperatingSystemFamily.LINUX,
      },
      taskRole,
    });

    const container = taskDef.addContainer('app', {
      image: ecs.ContainerImage.fromEcrRepository(repo, 'latest'),
      logging: ecs.LogDrivers.awsLogs({ streamPrefix: 'app', logGroup }),
      environment: {
        BEDROCK_MODEL_ID: props.modelId,
        AWS_REGION: this.region,
        TENANT_KEYS_SECRET_ID: tenantKeys.secretName,
        WEB_CONCURRENCY: '2',
      },
      portMappings: [{ containerPort: 8000, protocol: ecs.Protocol.TCP }],
    });

    // ---- ALB security ----------------------------------------------------
    // NOTE: ALB is currently open to the internet; tenant API key auth gates
    // requests at the app layer. To force traffic through CloudFront, set the
    // context value `cloudfrontPrefixListId` to the
    // `com.amazonaws.global.cloudfront.origin-facing` prefix list ID for this
    // region (publicly documented in AWS docs) and only that prefix list will
    // be allowed ingress.
    const albSg = new ec2.SecurityGroup(this, 'AlbSg', {
      vpc,
      allowAllOutbound: true,
      description: 'ALB SG',
    });
    const cfPrefixListId = this.node.tryGetContext('cloudfrontPrefixListId') as string | undefined;
    if (cfPrefixListId) {
      albSg.addIngressRule(
        ec2.Peer.prefixList(cfPrefixListId),
        ec2.Port.tcp(80),
        'CloudFront origin-facing only',
      );
    } else {
      albSg.addIngressRule(ec2.Peer.anyIpv4(), ec2.Port.tcp(80), 'public (auth gated)');
    }

    const alb = new elbv2.ApplicationLoadBalancer(this, 'Alb', {
      vpc,
      internetFacing: true,
      securityGroup: albSg,
      idleTimeout: cdk.Duration.seconds(180),
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
    });

    const serviceSg = new ec2.SecurityGroup(this, 'ServiceSg', {
      vpc,
      allowAllOutbound: true,
      description: 'Fargate task SG (ingress from ALB only)',
    });
    serviceSg.addIngressRule(albSg, ec2.Port.tcp(8000), 'from ALB');

    const service = new ecs.FargateService(this, 'Service', {
      cluster,
      taskDefinition: taskDef,
      desiredCount: 1,
      assignPublicIp: true, // public subnets, no NAT
      securityGroups: [serviceSg],
      vpcSubnets: { subnetType: ec2.SubnetType.PUBLIC },
      circuitBreaker: { rollback: true },
      minHealthyPercent: 50,
      maxHealthyPercent: 200,
    });

    const listener = alb.addListener('HttpListener', {
      port: 80,
      open: false, // SG controls who can reach it (CloudFront only)
    });

    listener.addTargets('AppTarget', {
      port: 8000,
      protocol: elbv2.ApplicationProtocol.HTTP,
      targets: [service],
      deregistrationDelay: cdk.Duration.seconds(15),
      healthCheck: {
        path: '/v1/models',
        healthyHttpCodes: '200,401', // 401 also means the app is up; we just lack creds for the health probe
        interval: cdk.Duration.seconds(30),
        timeout: cdk.Duration.seconds(5),
      },
    });

    // ---- CloudFront in front of the ALB ---------------------------------
    const distribution = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'OpenAI-compatible API (Bedrock)',
      defaultBehavior: {
        origin: new origins.LoadBalancerV2Origin(alb, {
          protocolPolicy: cloudfront.OriginProtocolPolicy.HTTP_ONLY,
          readTimeout: cdk.Duration.seconds(60),
          keepaliveTimeout: cdk.Duration.seconds(60),
        }),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
        cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
        originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
        compress: true,
      },
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100, // cheaper edges; bump if you serve globally
    });

    // ---- Outputs --------------------------------------------------------
    new cdk.CfnOutput(this, 'AlbDnsName', { value: alb.loadBalancerDnsName });
    new cdk.CfnOutput(this, 'CloudFrontDomain', { value: distribution.distributionDomainName });
    new cdk.CfnOutput(this, 'TenantKeysSecretArn', { value: tenantKeys.secretArn });
    new cdk.CfnOutput(this, 'EcsClusterName', { value: cluster.clusterName });
    new cdk.CfnOutput(this, 'EcsServiceName', { value: service.serviceName });
  }
}
