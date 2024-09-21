import * as cdk from "aws-cdk-lib";
import { Construct } from "constructs";
import * as lambda from "aws-cdk-lib/aws-lambda";
import { EventbridgeToLambda } from "@aws-solutions-constructs/aws-eventbridge-lambda";
import * as events from "aws-cdk-lib/aws-events";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as secrets from "aws-cdk-lib/aws-secretsmanager";
import * as python from "@aws-cdk/aws-lambda-python-alpha";
import * as sqs from "aws-cdk-lib/aws-sqs";
import * as lambdaEventSources from "aws-cdk-lib/aws-lambda-event-sources";
import * as sns from "aws-cdk-lib/aws-sns";
import * as snsSubscriptions from "aws-cdk-lib/aws-sns-subscriptions";
import * as ecr from "aws-cdk-lib/aws-ecr";

export class GucAssistantStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const usersTable = new dynamodb.Table(this, "Users", {
      tableName: "Users",
      partitionKey: { name: "username", type: dynamodb.AttributeType.STRING },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const courseItemsTable = new dynamodb.Table(this, "CourseItems", {
      tableName: "CourseItems",
      partitionKey: { name: "username", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "url", type: dynamodb.AttributeType.STRING },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const mailsTable = new dynamodb.Table(this, "Mails", {
      tableName: "Mails",
      partitionKey: { name: "username", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "mailId", type: dynamodb.AttributeType.STRING },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    const courseAnnouncements = new dynamodb.Table(
      this,
      "CourseAnnouncements",
      {
        tableName: "CourseAnnouncements",
        partitionKey: { name: "username", type: dynamodb.AttributeType.STRING },
        sortKey: { name: "courseId", type: dynamodb.AttributeType.STRING },
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }
    );

    const usersSnsTopic = new sns.Topic(this, "UsersSnsTopic", {
      topicName: "UsersSnsTopic",
    });

    const scrapCmsQueue = new sqs.Queue(this, "ScrapCmsQueue", {
      queueName: "ScrapCmsQueue",
      visibilityTimeout: cdk.Duration.seconds(60),
    });

    const scrapMailQueue = new sqs.Queue(this, "ScrapMailQueue", {
      queueName: "ScrapMailQueue",
      visibilityTimeout: cdk.Duration.seconds(60),
    });

    usersSnsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(scrapCmsQueue)
    );

    usersSnsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(scrapMailQueue)
    );

    const courseItemsSnsTopic = new sns.Topic(this, "CourseItemsSnsTopic", {
      topicName: "CourseItemsSnsTopic",
    });

    const addCourseItemsToTodoistQueue = new sqs.Queue(
      this,
      "AddCourseItemsToTodoistQueue",
      {
        queueName: "AddCourseItemsToTodoistQueue",
        visibilityTimeout: cdk.Duration.seconds(60),
      }
    );

    courseItemsSnsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(addCourseItemsToTodoistQueue)
    );

    const sendEmailNotificationQueue = new sqs.Queue(
      this,
      "SendEmailNotificationQueue",
      {
        queueName: "SendEmailNotificationQueue",
        visibilityTimeout: cdk.Duration.seconds(60),
      }
    );

    courseItemsSnsTopic.addSubscription(
      new snsSubscriptions.SqsSubscription(sendEmailNotificationQueue)
    );

    const fetchUsersLambda = new python.PythonFunction(
      this,
      "FetchUsersLambda",
      {
        runtime: lambda.Runtime.PYTHON_3_11,
        entry: "lambda/guc-assistant",
        index: "guc_assistant.py",
        handler: "fetch_users",
        bundling: {
          assetExcludes: [".venv", "requirements.txt"],

        },
        timeout: cdk.Duration.seconds(60),
        environment: {
          USERS_TOPIC_ARN: usersSnsTopic.topicArn,
        },
      }
    );

    new EventbridgeToLambda(this, "EventbridgeToLambda", {
      existingLambdaObj: fetchUsersLambda,
      eventRuleProps: {
        schedule: events.Schedule.rate(cdk.Duration.hours(1)),
      },
    });

    const registerUserLambda = new python.PythonFunction(
      this,
      "RegisterUserLambda",
      {
        runtime: lambda.Runtime.PYTHON_3_11,
        entry: "lambda/guc-assistant",
        index: "guc_assistant.py",
        handler: "register_user",
        bundling: {
          assetExcludes: [".venv"],
        },
        timeout: cdk.Duration.seconds(60),
        environment: {
          USERS_TOPIC_ARN: usersSnsTopic.topicArn,
        },
      }
    );

    const scrapUpdatesForUserLambda = new python.PythonFunction(
      this,
      "ScrapUpdatesForUserLambda",
      {
        runtime: lambda.Runtime.PYTHON_3_11,
        entry: "lambda/guc-assistant",
        index: "guc_assistant.py",
        handler: "scrap_updates_for_user",
        bundling: {
          assetExcludes: [".venv"],
        },
        timeout: cdk.Duration.seconds(60),
        environment: {
          COURSE_ITEMS_TOPIC_ARN: courseItemsSnsTopic.topicArn,
          GMAIL_SENDER_EMAIL: process.env.GMAIL_SENDER_EMAIL || "",
          GMAIL_SENDER_PASSWORD: process.env.GMAIL_SENDER_PASSWORD || "",
        },
      }
    );

    scrapUpdatesForUserLambda.addEventSource(
      new lambdaEventSources.SqsEventSource(scrapCmsQueue, {})
    );

    const scrapMailForUserLambda = new python.PythonFunction(
      this,
      "ScrapMailForUserLambda",
      {
        runtime: lambda.Runtime.PYTHON_3_11,
        entry: "lambda/guc-assistant",
        index: "guc_assistant.py",
        handler: "scrap_mail",
        bundling: {
          assetExcludes: [".venv"],
        },
        timeout: cdk.Duration.seconds(60),
      }
    );

    scrapMailForUserLambda.addEventSource(
      new lambdaEventSources.SqsEventSource(scrapMailQueue, {})
    );

    const addTodoistTask = new python.PythonFunction(
      this,
      "AddTodoistTaskLambda",
      {
        runtime: lambda.Runtime.PYTHON_3_11,
        entry: "lambda/guc-assistant",
        index: "guc_assistant.py",
        handler: "add_todoist_task",
        bundling: {
          assetExcludes: [".venv"],
        },
        timeout: cdk.Duration.seconds(60),
      }
    );

    const sendEmailNotification = new python.PythonFunction(
      this,
      "SendEmailNotificationLambda",
      {
        runtime: lambda.Runtime.PYTHON_3_11,
        entry: "lambda/guc-assistant",
        index: "guc_assistant.py",
        handler: "send_email_notification",
        bundling: {
          assetExcludes: [".venv"],
        },
        timeout: cdk.Duration.seconds(60),
        environment: {
          GMAIL_SENDER_EMAIL: process.env.GMAIL_SENDER_EMAIL || "",
          GMAIL_SENDER_PASSWORD: process.env.GMAIL_SENDER_PASSWORD || "",
        },
      }
    );

    addTodoistTask.addEventSource(
      new lambdaEventSources.SqsEventSource(addCourseItemsToTodoistQueue, {})
    );

    sendEmailNotification.addEventSource(
      new lambdaEventSources.SqsEventSource(sendEmailNotificationQueue, {})
    );

    usersSnsTopic.grantPublish(fetchUsersLambda);
    usersSnsTopic.grantPublish(registerUserLambda);
    courseItemsSnsTopic.grantPublish(scrapUpdatesForUserLambda);

    usersTable.grantReadData(fetchUsersLambda);
    usersTable.grantReadWriteData(registerUserLambda);

    courseItemsTable.grantFullAccess(scrapUpdatesForUserLambda);
    courseAnnouncements.grantFullAccess(scrapUpdatesForUserLambda);
    mailsTable.grantFullAccess(scrapMailForUserLambda);

    const fetchUsersFunctionUrl = new lambda.FunctionUrl(
      this,
      "FetchUsersUrl",
      {
        function: fetchUsersLambda,
        authType: lambda.FunctionUrlAuthType.NONE,
      }
    );

    const registerUserFunctionUrl = new lambda.FunctionUrl(
      this,
      "RegisterUserUrl",
      {
        function: registerUserLambda,
        authType: lambda.FunctionUrlAuthType.NONE,
      }
    );

    new cdk.CfnOutput(this, "FetchUsersUrlOutput", {
      value: fetchUsersFunctionUrl.url,
    });

    new cdk.CfnOutput(this, "RegisterUserUrlOutput", {
      value: registerUserFunctionUrl.url,
    });
  }
}
