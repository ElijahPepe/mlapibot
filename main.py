import praw
import logging
import re
import requests
import json
import tempfile
import os, sys, time
import ocr, scam

from datetime import datetime
from praw.models import Message, Comment
from webhook import WebhookSender

os.chdir(os.path.join(os.getcwd(), "data"))

valid_extensions = [".png", ".jpg", ".jpeg"]
def load_scams():
    global SCAMS, THRESHOLD
    SCAMS = []
    THRESHOLD = 0.9

    try:
        with open("scams.json") as f:
            rawText = f.read()
        obj = json.loads(rawText)
        for scm in obj["scams"]:
            SCAMS.append(scam.Scam(scm["name"], scm["reason"], scm["text"]))
    except Exception as e:
        logging.error(e)
        SCAMS = []

    if len(SCAMS) == 0:
        logging.error("Refusing to continue: no scams loaded")
        exit(1)
def load_reddit():
    global reddit, subReddit
    reddit = praw.Reddit("bot1", user_agent="script:mlapiOCR:v0.0.1 (by /u/DarkOverLordCO)")
    subReddit = reddit.subreddit("discordapp")

def setup():
    global webHook, WEBHOOK_URL, latest_done, handled_messages, handled_posts,\
        TEMPLATE

    load_scams()
    load_reddit()

    try:
        with open("webhook.txt", "r") as f:
            WEBHOOK_URL = f.read()
    except Exception as e:
        logging.error(e)
        logging.warning("Disabling webhook sending as missing URL")
        WEBHOOK_URL = None

    webHook = WebhookSender(WEBHOOK_URL, subReddit.display_name)

    latest_done = []
    handled_posts = []
    handled_messages = []

    try:
        with open("save.txt", "r") as f:
            for x in f:
                latest_done.append(x.rstrip())
                if len(latest_done) > 25:
                    latest_done.pop(0)
    except Exception as e:
        print(e)
        latest_done = []
        logging.warn("Failed to load previously handled things")

    TEMPLATE = ""
    try:
        with open("template.md", "r") as f:
            TEMPLATE = f.read()
    except Exception as e:
        logging.error(e)

    if not TEMPLATE:
        logging.error("Refusing to continue: Template is empty")
        exit(1)


def saveLatest(thingId):
    latest_done.append(thingId)
    if len(latest_done) > 25:
        latest_done.pop(0)
    with open("save.txt", "w") as f:
        f.write("\n".join(latest_done))

def loopInbox():
    unread_messages = []
    for item in reddit.inbox.unread(limit=None):
        if isinstance(item, Message):
            unread_messages.append(item)
        if isinstance(item, Comment):
            unread_messages.append(item)
    reddit.inbox.mark_read(unread_messages)
    for x in unread_messages:
        webHook.sendInboxMessage(x)
        logging.warning("%s: %s", x.author.name, str(str(x.body).encode("utf-8")))

def getFileName(url):
        index = url.rfind('/')
        if index == -1:
            index = url.rfind('\\')
        filename = url[index+1:]
        thing = filename.find('?')
        if thing != -1:
            filename = filename[:thing]
        return filename


def validImage(filename):
    for ext in valid_extensions:
        if filename.endswith(ext):
            return True
    return False

def extractURLS(post):
    any_url = []
    if validImage(post.url):
        any_url.append(post.url)
    if post.is_self:
        matches = re.findall("https?:\/\/[\w\-%\.\/\=\?\&]+",
            str(str(post.selftext).encode("utf-8")))
        for x in matches:
            if validImage(getFileName(x)):
                any_url.append(x)
    return any_url

def getScams(array):
    scamResults = {}
    for x in SCAMS:
        result = x.PercentageMatch(array)
        logging.debug("{0}: {1}".format(x, result))
        if result > THRESHOLD:
            scamResults[x] = result
    return scamResults


def handleFileName(path, filename):
    text = ocr.getTextFromPath(path, filename)
    text = text.lower()
    array = re.findall(r"[\w']+", text)
    if len(sys.argv) > 1:
        logging.info(array)
    return getScams(array)

def handleUrl(url):
    filename = getFileName(url)
    r = requests.get(url, allow_redirects=True)
    if not r.ok:
        print("=== err")
        print(url)
        print(r)
        print("===")
        return
    tempPath = os.path.join(tempfile.gettempdir(), filename)
    print(tempPath)
    with open(tempPath, "wb") as f:
        f.write(r.content)
    return handleFileName(tempPath, filename)

def handlePost(post):
    urls = extractURLS(post)
    logging.info(str(urls))
    for url in urls:
        results = handleUrl(url)
        if len(results) > 0:
            text = ""
            for scam, confidence in results.items():
                text += scam.Name + ": " + scam.Reason + "\r\n\r\n"
                print(scam.Name, confidence)
            built = TEMPLATE.format(text)
            if os.name != "nt":
                post.reply(built)
            webHook.sendSubmission(post, text)
            logging.info("Replied to: " + str(str(post.title).encode("utf-8")))
            return



def loopPosts():
    for post in subReddit.new(limit=25):
        if post.name in latest_done:
            break # Since we go new -> old, don't go any further into old
        logging.info("New: " + str(str(post.title).encode("utf-8")))
        saveLatest(post.name)
        handlePost(post)



load_scams()
if __name__ == "__main__":
    logging.basicConfig(filename='mlapi.log', level=logging.INFO)
    logging.getLogger().addHandler(logging.StreamHandler(sys.stdout))
    if len(sys.argv) == 2:
        path = sys.argv[1]
        if path.startswith("http"):
            print(handleUrl(path))
        else:
            fileName = getFileName(path)
            print(handleFileName(path, fileName))
        exit(0)
    setup()
    doneOnce = False
    while True:
        if not doneOnce:
            logging.info("Starting loop")
        loopPosts()
        if not doneOnce:
            logging.info("Half way loop")
        loopInbox()
        if not doneOnce:
            logging.info("Finished first loop")
            doneOnce = True



