#!/usr/bin/env python
# -*- coding: utf-8 -*-
__author__ = "marko"

import sys, os, datetime, smtplib, json
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import codecs
from collections import OrderedDict
import traceback

import sqlalchemy
from sqlalchemy import and_

import jinja2
import html2text

import model
from model import Forum_post, Forum_section, Forum_group, Forum_topic, User, Blog_post, Event, Comment, Comment_entity
import configuration


def seconds_since_epoch(a_datetime):
    """
    :param a_datetime: datetime.datetime
    :return: long
    """
    """
    :param a_datetime: datetime.datetime
    :return: long
    """
    epoch = datetime.datetime.utcfromtimestamp(0)
    return long((a_datetime - epoch).total_seconds())


datetime_format = "%Y-%m-%dT%H-%M-%S.%f"


def read_last_insterval_end():
    if not os.path.exists(os.path.dirname(configuration.state_path)):
        sys.stderr.write("The directory %s of the log file %s must exist." % (
            os.path.dirname(configuration.state_path), configuration.state_path))
        sys.exit(1)

    if not os.path.exists(configuration.state_path):
        fid = open(configuration.state_path, "w")
        fid.close()

    lines = []
    fid = open(configuration.state_path, "r")
    lines = [line for line in fid.readlines() if not line.startswith("#") and not len(line.strip()) == 0]
    fid.close()

    def extract_timestamps(enumerated_line):
        (line_number, line) = enumerated_line
        try:
            entry = json.loads(line)
            return datetime.datetime.strptime(entry["interval_end"], datetime_format)
        except Exception as exception:
            raise RuntimeError("Line %d in log file %s is invalid: %s." % (
                line_number, configuration.state_path, repr(exception)
            ))

    timestamps = [configuration.exclude_content_before] + map(extract_timestamps, enumerate(lines))
    return max(timestamps)


def update(path, entry):
    """
    Updates either the state of the application or the log.

    :param path: path to the state or log. Must be either configuration.state_path or configuration.log_path.
    :param entry: dictionary representing the update. Will be converted to JSON.
    """
    assert (path in [configuration.state_path, configuration.log_path])

    try:
        with open(path, "a") as fid:
            fid.write(json.dumps(entry) + "\n")

    except IOError:
        raise RuntimeError("The file %s could not be opened for appending." % path)


class Entity_digest(object):
    def __init__(self, title, users, first, last, url):
        """
        :param title: str
        :param users: list[str]
        :param first: long
        :param last: long
        :param url: string
        """
        self.users = users
        self.first = first
        self.last = last
        self.url = url

    def append(self, user, timestamp):
        """
        :param user: str
        :param timestamp: long
        """
        if user not in self.users:
            self.users.append(user)

        self.first = min(self.first, timestamp)
        self.last = max(self.last, timestamp)


def digest(now):
    """
    Produces and sends the digest to the recipients.

    :param now: datetime representing the time when the application started.
    """
    interval_begin = read_last_insterval_end()
    interval_end = now

    engine = sqlalchemy.create_engine(configuration.database_url)

    model.Base.metadata.bind = engine
    DBSession = sqlalchemy.orm.sessionmaker(bind=engine)
    session = DBSession()

    # Cache
    session.query(Forum_section).all()
    session.query(Forum_group).all()
    session.query(Forum_topic).all()
    session.query(User).all()

    # Load template
    script_dir = os.path.dirname(os.path.realpath(__file__))
    template_path = os.path.join(script_dir, "mail_template.html")
    fid = codecs.open(template_path, "r", "utf-8")
    template = jinja2.Template(fid.read())
    fid.close()

    # Digest forum posts
    topic_digests = OrderedDict()
    forum_post_count = 0
    for post in session.query(Forum_post).filter(
            and_(Forum_post.create_stamp >= seconds_since_epoch(interval_begin),
                 Forum_post.create_stamp < seconds_since_epoch(interval_end))).order_by(Forum_post.create_stamp):

        # Compare against True instead of False or using "not", since is_private and is_hidden
        # can be None.
        if post.topic.group is None or (
                post.topic.group.is_private != True and post.topic.group.section.is_hidden != True):
            forum_post_count += 1

            topic_title = post.topic.title
            if topic_title not in topic_digests:
                topic_digests[topic_title] = Entity_digest(
                        title=topic_title,
                        users=[post.user.username],
                        first=post.create_stamp,
                        last=post.create_stamp,
                        url="%s/forum/topic/%s" % (configuration.url_prefix, post.topic.id))
            else:
                topic_digest = topic_digests[topic_title]
                topic_digest.append(post.user.username, post.create_stamp)

    # Digest blog posts
    blog_posts = []
    for post in session.query(Blog_post).filter(
            and_(Blog_post.timestamp >= seconds_since_epoch(interval_begin),
                 Blog_post.timestamp < seconds_since_epoch(interval_end)),
                    Blog_post.privacy == "everybody",
                    Blog_post.is_draft == False).order_by(Blog_post.timestamp):
        blog_posts.append({
            "username": post.user.username,
            "date": datetime.datetime.fromtimestamp(post.timestamp).strftime("%Y-%m-%d %H:%M"),
            "title": post.title,
            "url": "%s/blogs/%s" % (configuration.url_prefix, post.id)
        })

    # Digest events
    events = []
    for event in session.query(Event).filter(
            and_(Event.create_timestamp >= seconds_since_epoch(interval_begin),
                 Event.create_timestamp < seconds_since_epoch(interval_end)),
                    Event.who_can_view == 1).order_by(Event.create_timestamp):
        events.append({
            "username": event.user.username,
            "date": datetime.datetime.fromtimestamp(event.create_timestamp).strftime("%Y-%m-%d %H:%M"),
            "title": event.title,
            "url": "%s/event/%s" % (configuration.url_prefix, event.id)
        })

    # Digest comments to blog posts
    blog_post_comments = OrderedDict()
    for comment in session.query(Comment).filter(
            and_(Comment.create_stamp >= seconds_since_epoch(interval_begin),
                 Comment.create_stamp < seconds_since_epoch(interval_end))).order_by(Comment.create_stamp):

        if comment.comment_entity.active != 1 or comment.comment_entity.entity_type != "blog-post":
            continue

        blog_post = session.query(Blog_post).filter(Blog_post.id == comment.comment_entity.entity_id).one()
        title = blog_post.title

        if title not in blog_post_comments:
            blog_post_comments[title] = Entity_digest(
                    title=title,
                    users=[comment.user.username],
                    first=comment.create_stamp,
                    last=comment.create_stamp,
                    url="%s/blogs/%s" % (configuration.url_prefix, blog_post.id)
            )
        else:
            blog_post_comments[title].append(comment.user.username, comment.create_stamp)

    # Digest comments to events
    event_comments = OrderedDict()
    for comment in session.query(Comment).filter(
            and_(Comment.create_stamp >= seconds_since_epoch(interval_begin),
                 Comment.create_stamp < seconds_since_epoch(interval_end))).order_by(Comment.create_stamp):

        if comment.comment_entity.active != 1 or comment.comment_entity.entity_type != "event":
            continue

        events = session.query(Event).filter(Event.id == comment.comment_entity.entity_id).all()
        if len(events) == 0:
            # event was probably deleted, or the database is in inconsistent state.
            # skip it.
            continue

        # ignore the multiplicity of the events.
        event = events[0]

        title = event.title

        if title not in event_comments:
            event_comments[title] = Entity_digest(
                    title=title,
                    users=[comment.user.username],
                    first=comment.create_stamp,
                    last=comment.create_stamp,
                    url="%s/event/%s" % (configuration.url_prefix, event.id)
            )
        else:
            event_comments[title].append(comment.user.username, comment.create_stamp)

    message = template.render(
            topic_digests=topic_digests,
            blog_posts=blog_posts,
            events=events,
            blog_post_comments=blog_post_comments,
            event_comments=event_comments,
            admin_email=configuration.admin_email).encode("UTF-8")

    # Send digests
    if len(message) > configuration.max_message_size:
        raise RuntimeError("The message size (== %d) exeeds the maximum message size (== %d)." % (
            len(message), configuration.max_message_size))

    recipient_list = []
    if configuration.send_to_all_oxwall_users == True:
        for user in session.query(User).all():
            recipient_list.append(user.email)

    recipient_list.extend(configuration.additional_recipients)
    excluded_recipient_set = set(configuration.excluded_recipients)
    recipient_list = [recipient for recipient in recipient_list if recipient not in excluded_recipient_set]

    s = None
    if not configuration.smtp.with_ssl:
        s = smtplib.SMTP(host=configuration.smtp.host, port=configuration.smtp.port)
    else:
        s = smtplib.SMTP_SSL(host=configuration.smtp.host, port=configuration.smtp.port)

    if configuration.smtp.user is not None:
        s.login(user=configuration.smtp.user, password=configuration.smtp.password)

    for recipient in recipient_list:
        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = "Nena1 Oxwall Zusammenfassung von %s bis %s" % (
                interval_begin.strftime("%Y-%m-%d %H:%M"),
                interval_end.strftime("%Y-%m-%d %H:%M")
            )
            msg["From"] = configuration.sender
            msg["To"] = recipient
            msg.attach(MIMEText(
                ("(Dein Email-Klient kann leider keine HTML-Nachrichten anzeigen. " +
                "Wir haben deswegen den Digest für Dich in ein leserliches Text-Format umgewandelt.)\n\n").decode('utf8') +
                html2text.html2text(message.decode('utf8')),
                "plain", "utf-8"))
            msg.attach(MIMEText(message, "html", "utf-8"))

            s.sendmail(configuration.sender, recipient, msg.as_string())
        except Exception as e:
            update(configuration.log_path,
               OrderedDict([
                   ("date", now.strftime(datetime_format)),
                   ("level", "error"),
                   ("message", str(e) + "; did not block"),
                   ("stacktrace", traceback.format_exc().split("\n"))
               ]))
    s.quit()

    # Update the checkpoint.
    update(configuration.state_path,
           OrderedDict([
               ("interval_begin", interval_begin.strftime(datetime_format)),
               ("interval_end", interval_end.strftime(datetime_format)),
               ("forum_post_count", forum_post_count),
               ("blog_post_count", len(blog_posts)),
               ("event_count", len(events)),
               ("message_size", len(message)),
               ("recipient_count", len(recipient_list))
           ]))


def main():
    if len(sys.argv) != 1:
        sys.stderr.write("Usage: launch.py {no arguments}")
        sys.exit(1)

    now = datetime.datetime.utcnow()
    try:
        digest(now)

        update(configuration.log_path,
               OrderedDict([
                   ("date", now.strftime(datetime_format)),
                   ("level", "info"),
                   ("message", "Terminated without error.")
               ]))


    except Exception as e:
        update(configuration.log_path,
               OrderedDict([
                   ("date", now.strftime(datetime_format)),
                   ("level", "fatal"),
                   ("message", "Terminated with fatal error: " + str(e)),
                   ("stacktrace", traceback.format_exc().split("\n"))
               ]))


if __name__ == "__main__":
    main()
