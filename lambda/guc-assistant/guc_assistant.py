from dataclasses import dataclass
import dataclasses
from datetime import datetime
from typing import Generator, Optional
import boto3
import json
import guc_cms_scrapper
from todoist_api_python.api import TodoistAPI
import os
from dacite import Config, from_dict
import smtplib
from email.mime.text import MIMEText
from guc_mail_scrapper import GucMailScrapper

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")
sns = boto3.client("sns")

USERS_TOPIC_ARN = os.getenv("USERS_TOPIC_ARN")
COURSE_ITEMS_TOPIC_ARN = os.getenv("COURSE_ITEMS_TOPIC_ARN")

SCRAP_CMS_QUEUE = os.getenv("SCRAP_CMS_QUEUE")
SCRAP_MAIL_QUEUE = os.getenv("SCRAP_MAIL_QUEUE")

ADD_COURSE_ITEMS_TO_TODOIST_QUEUE = os.getenv("ADD_COURSE_ITEMS_TO_TODOIST_QUEUE")
SEND_EMAIL_NOTIFICATION_QUEUE = os.getenv("SEND_EMAIL_NOTIFICATION_QUEUE")

sender_email = os.getenv("GMAIL_SENDER_EMAIL")
sender_password = os.getenv("GMAIL_SENDER_PASSWORD")


@dataclass
class EmailNotifications:
    course_announcements: bool = True
    course_items: bool = True
    mails: bool = True


@dataclass
class User:
    username: str
    password: str
    todoist_token: str
    email: str
    todoist_project_id: Optional[str] = None
    course_id_to_todoist_section_id: dict[str, str] = dataclasses.field(default_factory=dict)
    courses: list[guc_cms_scrapper.CourseMetadata] = dataclasses.field(default_factory=list)
    email_notifications: EmailNotifications = dataclasses.field(default_factory=EmailNotifications)
    add_course_items_to_todoist_enabled: bool = True


def get_users() -> Generator[list[User], None, None]:
    table = dynamodb.Table("Users")
    response = table.scan()

    while True:
        yield [User(**item) for item in response["Items"]]

        if "LastEvaluatedKey" not in response:
            break

        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])


def fetch_users(event, context):
    for users in get_users():
        sns.publish_batch(
            TopicArn=USERS_TOPIC_ARN,
            PublishBatchRequestEntries=[
                {
                    "Id": str(i),
                    "Message": json.dumps(dataclasses.asdict(user)),
                }
                for i, user in enumerate(users)
            ],
        )


def register_user(event, context):
    body = json.loads(event["body"])

    if "username" not in body or "password" not in body or "todoist_token" not in body or "email" not in body:
        return {
            "statusCode": 400,
            "body": "Missing required fields",
        }
    try:
        scrapper = guc_cms_scrapper.GucCmsScrapper(body["username"], body["password"])
        courses = scrapper.get_courses()

        todoist = TodoistAPI(body["todoist_token"])
        todoist_project = todoist.add_project("GUC")

        course_todoist_section_ids = {
            course.id: todoist.add_section(course.name, project_id=todoist_project.id).id for course in courses
        }

        table = dynamodb.Table("Users")

        email_notifications = body.get("email_notifications", {})

        user = {
            "username": body["username"],
            "password": body["password"],
            "todoist_token": body["todoist_token"],
            "email": body["email"],
            "todoist_project_id": todoist_project.id,
            "course_id_to_todoist_section_id": course_todoist_section_ids,
            "courses": [dataclasses.asdict(course) for course in courses],
            "email_notifications": {
                "course_announcements": email_notifications.get("course_announcements", True),
                "course_items": email_notifications.get("course_items", True),
                "mails": email_notifications.get("mails", True),
            },
            "add_course_items_to_todoist_enabled": body.get("add_course_items_to_todoist_enabled", True),
        }
        table.put_item(Item=user)

        sns.publish(
            TopicArn=USERS_TOPIC_ARN,
            Message=json.dumps(user),
        )

        return {
            "statusCode": 200,
            "body": json.dumps(
                {
                    "project_id": todoist_project.id,
                    "course_id_to_todoist_section_id": course_todoist_section_ids,
                }
            ),
        }

    except guc_cms_scrapper.InvalidCredentialsError:
        return {
            "statusCode": 400,
            "body": "Invalid GUC username or password",
        }
    except Exception as e:
        return {
            "statusCode": 500,
            "body": str(e),
        }


def send_announcement_email(user_email: str, announcement: str, course: guc_cms_scrapper.CourseMetadata):
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.set_debuglevel(1)
        server.starttls()
        server.login(sender_email, sender_password)

        msg = MIMEText(announcement, "html")
        msg["Subject"] = f"[Announcement] {course.name}"
        msg["From"] = sender_email

        server.sendmail(sender_email, user_email, msg.as_string())


def scrap_updates_for_user(event, context):
    for record in event["Records"]:
        body = json.loads(json.loads(record["body"])["Message"])
        user = from_dict(User, body)
        scrapper = guc_cms_scrapper.GucCmsScrapper(user.username, user.password)

        for course in user.courses:
            course_data = scrapper.get_course_data(course.id, course.semester)

            if (
                user.email_notifications.course_announcements
                and course_data.announcements is not None
                and course_data.announcements.strip() != ""
            ):
                try:
                    dynamodb.Table("CourseAnnouncements").put_item(
                        Item={
                            "username": user.username,
                            "courseId": course.id,
                            "announcement": course_data.announcements,
                        },
                        ConditionExpression="attribute_not_exists(username) OR announcement <> :announcement",
                        ExpressionAttributeValues={
                            ":announcement": course_data.announcements,
                        },
                    )

                    send_announcement_email(user.email, course_data.announcements, course)
                except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                    pass

            for week in course_data.weeks:
                for item in week.items:
                    try:
                        dynamodb.Table("CourseItems").put_item(
                            Item={
                                "username": user.username,
                                "url": item.url,
                                "title": item.title,
                                "description": item.description,
                                "type": item.type,
                                "course_id": course.id,
                                "week_start_date": week.start_date.isoformat(),
                            },
                            ConditionExpression="attribute_not_exists(username)",
                        )

                        sns.publish(
                            TopicArn=COURSE_ITEMS_TOPIC_ARN,
                            Message=json.dumps(
                                {
                                    "user_data": dataclasses.asdict(user),
                                    "week_data": dataclasses.asdict(week),
                                    "course_data": dataclasses.asdict(course),
                                    "item_data": dataclasses.asdict(item),
                                },
                                default=str,
                            ),
                        )
                    except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                        pass


def add_todoist_task(event, context):
    for record in event["Records"]:
        data = json.loads(json.loads(record["body"])["Message"])
        user = from_dict(User, data["user_data"])

        if not user.add_course_items_to_todoist_enabled:
            continue

        item = from_dict(
            guc_cms_scrapper.CourseItem, data["item_data"], config=Config(cast=[guc_cms_scrapper.CourseItemType])
        )
        course = from_dict(guc_cms_scrapper.CourseMetadata, data["course_data"])

        todoist = TodoistAPI(user.todoist_token)
        section_id = user.course_id_to_todoist_section_id[course.id]

        description = f"{item.description}\nAdded on: {datetime.now().isoformat()}"

        todoist.add_task(
            f"[{item.title}]({item.url})",
            project_id=user.todoist_project_id,
            section_id=section_id,
            description=description,
            due_string="today",
            labels=[item.type],
        )


def send_email_notification(event, context):
    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(sender_email, sender_password)

        for record in event["Records"]:
            data = json.loads(json.loads(record["body"])["Message"])
            user = from_dict(User, data["user_data"])

            if not user.email_notifications.course_items:
                continue

            item = from_dict(
                guc_cms_scrapper.CourseItem, data["item_data"], config=Config(cast=[guc_cms_scrapper.CourseItemType])
            )
            course = from_dict(guc_cms_scrapper.CourseMetadata, data["course_data"])

            msg = MIMEText(
                f"Course: {course.code} | {course.name}\n"
                + f"Item: {item.title}\n"
                + f"Description: {item.description}\n"
                + f"Type: {item.type}\n"
                + f"URL: {item.url}\n"
                + f"Week: {data['week_data']['start_date']}",
            )

            msg["Subject"] = f"{course.name} - {item.title}"
            msg["From"] = sender_email
            msg["To"] = user.email

            server.sendmail(sender_email, user.email, msg.as_string())


def scrap_mail(event, context):
    for record in event["Records"]:
        body = json.loads(json.loads(record["body"])["Message"])
        user = from_dict(User, body)

        if not user.email_notifications.mails:
            continue

        authenticated_session = GucMailScrapper.get_authenticated_session(user.username, user.password)
        scrapper = GucMailScrapper(authenticated_session)

        for page in range(1, scrapper.count_mail_pages() + 1):
            for mail_id in scrapper.get_mail_ids(page):
                try:
                    dynamodb.Table("Mails").put_item(
                        Item={"username": user.username, "mailId": mail_id},
                        ConditionExpression="attribute_not_exists(username)",
                    )

                    scrapper.forward_mail(mail_id, user.email)
                except dynamodb.meta.client.exceptions.ConditionalCheckFailedException:
                    pass
