import praw
import logging
import re
import requests
import json
import tempfile
import os, sys, time

from typing import List
from datetime import datetime
from praw.models import Message, Comment
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from urllib.parse import urlparse

import mlapi.ocr as ocr
from mlapi.models.response_builder import ResponseBuilder
from mlapi.models.scam import Scam
from mlapi.models.scam_encoder import ScamEncoder
from mlapi.webhook import WebhookSender

print(os.getcwd())
os.chdir(os.path.join(os.getcwd(), "data"))

ocr_scam_pattern = r"(?:\bhttps://)?[-A-Za-z0-9+&@#/%?=~_|!:,.;]+[-A-Za-z0-9+&@#/%=~_|]"
discord_invite_pattern = r"https:\/\/discord\.(?:gg|com\/invites)\/([A-Za-z0-9-]{5,16})"
valid_extensions = [".png", ".jpeg", ".jpg"]

def load_reddit():
    global reddit, subReddit, author
    author = "DarkOverLordCO"
    reddit = praw.Reddit("bot1", user_agent="script:mlapiOCR:v0.0.5 (by /u/" + author + ")")
    srName = "DiscordApp"
    if os.name == "nt":
        srName = "mlapi"
    subReddit = reddit.subreddit(srName)
def load_scams():
    global SCAMS, THRESHOLD
    SCAMS = []
    THRESHOLD = 0.9

    try:
        with open("scams.json") as f:
            rawText = f.read()
        obj = json.loads(rawText)
        for scm in obj["scams"]:
            template = scm.get("template", "default")
            upLow = "name" in scm
            name = scm["name" if upLow else "Name"]
            ocr = scm.get("ocr" if upLow else "OCR", [])
            title = scm.get("title" if upLow else "Title", [])
            body = scm.get("body" if upLow else "Body", [])
            blacklist = scm.get("blacklist" if upLow else "Blacklist", [])
            selfposts = scm.get("ignore_self_posts", False)
            scam = Scam(name, ocr, title, body, blacklist, selfposts, template)
            SCAMS.append(scam)
    except Exception as e:
        logging.error(e)
        print(e)
        SCAMS = []

    if len(SCAMS) == 0:
        logging.error("Refusing to continue: no scams loaded")
        exit(1)

def save_scams():
    try:
        content = json.dumps({"scams": SCAMS}, indent=4, cls=ScamEncoder)
        with open("scams.json", "w") as f:
            f.write(content)
    except Exception as e:
        logging.error(e)

def load_history():
    global HISTORY, HISTORY_TOTAL, TOTAL_CHECKS
    HISTORY = {}
    HISTORY_TOTAL = 0
    TOTAL_CHECKS = 0
    try:
        with open("history.json", "r") as f:
            content = f.read()
    except:
        return
    obj = json.loads(content)
    HISTORY = obj["history"]
    HISTORY_TOTAL = obj["scams"]
    TOTAL_CHECKS = obj["total"]

def save_history():
    content = json.dumps({
        "history": HISTORY,
        "total": TOTAL_CHECKS,
        "scams": HISTORY_TOTAL
    })
    try:
        with open ("history.json", "w") as f:
            f.write(content)
    except Exception as e:
        logging.error(e)
        return
def load_templates():
    global TEMPLATES
    TEMPLATES = {}
    files = os.listdir("templates")
    for x in files:
        if x.endswith(".md"):
            name = x[:-3]
            print(name)
            with open("templates/" + x, "r") as f:
                TEMPLATES[name] = f.read()
    return TEMPLATES

def setup():
    global webHook, WEBHOOK_URL, latest_done, handled_messages, handled_posts,\
        TEMPLATES

    load_scams()
    load_reddit()

    try:
        with open("webhook.txt", "r") as f:
            WEBHOOK_URL = f.read().strip()
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
                if len(latest_done) > 50:
                    latest_done.pop(0)
    except Exception as e:
        logging.error(e)
        latest_done = []
        logging.warn("Failed to load previously handled things")

    try:
        TEMPLATES = load_templates()
    except Exception as e:
        logging.error(e)

    if not TEMPLATES:
        logging.error("Refusing to continue: Templates is empty")
        exit(1)

    load_history()

def requests_retry_session(
    retries=3,
    backoff_factor=2,
    status_forcelist=(500, 502, 504),
    session=None,
    ):
    session = session or requests.Session()
    retry = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session


def saveLatest(thingId):
    latest_done.append(thingId)
    if len(latest_done) > 250:
        latest_done.pop(0)
    with open("save.txt", "w") as f:
        f.write("\n".join(latest_done))

def addScam(content):
    lines = content.split("\n")
    name = lines[0]
    texts = lines[1:]
    scm = Scam(name, texts, None)
    SCAMS.append(scm)
    save_scams()

def handleUserMsg(post, isAdmin: bool) -> bool:
    text = post.body
    if text.startswith("/u/mlapibot "):
        text = text[len("/u/mlapibot "):]
        print(text)
    if text.startswith("https"):
        handlePost(post)
        return True
    elif isAdmin and isinstance(post, Message) and post.subject == "[add]":
        addScam(post.body)
        post.reply("Registered new scam; note: will not persist.")
        return True
    return False

def loopInbox():
    unread_messages = []
    for item in reddit.inbox.unread(limit=None):
        unread_messages.append(item)
    reddit.inbox.mark_read(unread_messages)
    for x in unread_messages:
        for property, value in vars(x).items():
            print(property, ":", value)
        webHook.sendInboxMessage(x)
        logging.warning("%s: %s", x.author.name, x.body)
        if isinstance(x, Message):
            done = handleUserMsg(x, x.author.name == author)
            if not done:
                x.reply("I was unable to determine what you wanted me to do, sorry.")
        elif isinstance(x, Comment):
            if not x.body.startswith("/u/mlapibot"):
                continue
            done = handleUserMsg(x, x.author.name == author)
            if not done:
                x.reply("Sorry! I'm not sure what you wanted me to do.")

def getFileName(url):
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None
    path = parsed.path
    index = path.rfind('/')
    if index == -1:
        index = path.rfind('\\')
    filename = path[index+1:]
    thing = filename.find('?')
    if thing != -1:
        filename = filename[:thing]
    return filename

def validImage(url):
    filename = getFileName(url)
    if filename is None:
        return False
    print(url + " -> " + filename)
    for ext in valid_extensions:
        if filename.endswith(ext):
            return True
    return False

def extractURLSText(text: str, pattern: str) -> List[str]:
    any_url = []
    matches = re.findall(pattern,
            text)
    for x in matches:
        any_url.append(x)
    return any_url

def extractURLS(post, pattern: str):
    any_url = []
    if isinstance(post, praw.models.Submission):
        if re.match(pattern, post.url) is not None:
            any_url.append(post.url)
        if post.is_self:
            any_url.extend(extractURLSText(post.selftext, pattern))
    elif isinstance(post, praw.models.Message):
        any_url.extend(extractURLSText(post.body, pattern))
    elif isinstance(post, praw.models.Comment):
        any_url.extend(extractURLSText(post.body, pattern))
    return any_url

def getScams(array : List[str], isSelfPost, builder: ResponseBuilder) -> ResponseBuilder:
    scamResults = {}
    for x in SCAMS:
        if x.IgnoreSelfPosts and isSelfPost:
            logging.debug("Skipping {0} as self post".format(x.Name))
            continue
        if x.IsBlacklisted(array, builder):
            logging.debug("Skipping {0} as blacklisted".format(x.Name))
            continue
        result = x.TestOCR(array, builder)
        logging.debug("{0}: {1}".format(x, result))
        if result > THRESHOLD:
            scamResults[x] = result
            builder.FormattedText = builder.TestGrounds
            #print(builder.FormattedText)
    builder.Add(scamResults)
    return builder

def getTextFromFileName(path: str, filename: str) -> List[str]:
    text = ocr.getTextFromPath(path, filename)
    text = text.lower()
    array = re.findall(r"[\w']+", text)
    if len(sys.argv) > 1:
        logging.info(" ".join(array))
        logging.info("==============")
    return array

def handleUrl(url: str) -> List[str]:
    filename = getFileName(url)
    try:
        r = requests_retry_session(retries=5).get(url)
    except Exception as x:
        logging.error('Could not handle url: {0} {1}'.format(url, x.__class__.__name__))
        print(str(x))
        try:
            e = webHook.getEmbed("Error With Image",
                str(x), url, x.__class__.__name__)
            logging.info(str(e))
            webHook._sendWebhook(e)
        except:
            pass
        return
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
    return getTextFromFileName(tempPath, filename)

def determineScams(post: praw.models.Submission) -> ResponseBuilder:
    scams = {}
    urls = extractURLS(post, ocr_scam_pattern)
    ocr_urls = [x for x in urls if validImage(x)]
    ocrArray = []
    builder = None
    for url in ocr_urls:
        wordArray = handleUrl(url)
        if wordArray is None or len(wordArray) == 0:
            continue

        if builder is None:
            builder = ResponseBuilder(THRESHOLD)
            builder.RecognisedText = " ".join(wordArray)
            builder.FormattedText = ">" + builder.RecognisedText.replace("\n", "\n>")
        else:
            text = " ".join(wordArray)
            builder.RecognisedText += "\r\n---\r\n" + text
            builder.FormattedText += "\r\n---\r\n>" + text.replace("\n", "\n>")

        ocrArray.extend(wordArray)


    if hasattr(post, "title"):
        titleText = post.title.lower()
    else:
        titleText = post.subject.lower()
    titleArray = re.findall(r"[\w']+", titleText)

    if hasattr(post, "selftext"):
        bodyText = post.selftext.lower()
    elif hasattr(post, "body"):
        bodyText = post.body.lower()
    else:
        bodyText = ""
    bodyArray = re.findall(r"[\w']+", bodyText)

    if builder == None:
        builder = ResponseBuilder(THRESHOLD)
        builder.RecognisedText = titleText + "  \r\n" + bodyText
        builder.FormattedText = ">" + builder.RecognisedText.replace("\n", "\n>")

    logging.info("Saw: " + builder.RecognisedText)

    totalArray = []
    totalArray.extend(titleArray)
    totalArray.extend(bodyArray)
    totalArray.extend(ocrArray)
    for x in SCAMS:
        if hasattr(post, "is_self"):
            if x.IgnoreSelfPosts and post.is_self:
                logging.info(f'Skipping {x.Name} due to selfpost')
                continue
        if x.IsBlacklisted(totalArray, builder):
            logging.info(f"Skipping {x.Name} due to blacklisted")
            builder.Remove(x)
            continue
        tit = x.TestTitle(titleArray, builder)
        bod = x.TestBody(bodyArray, builder)
        ocr = x.TestOCR(ocrArray, builder)
        if tit > THRESHOLD:
            builder.Add({x: tit})
        if bod > THRESHOLD:
            builder.Add({x: bod})
        if ocr > THRESHOLD:
            builder.Add({x: ocr})

    return builder



def handlePost(post: praw.models.Message, printRawTextOnPosts = False) -> ResponseBuilder:
    global TOTAL_CHECKS, HISTORY_TOTAL, HISTORY
    SUFFIXES = {1: 'st', 2: 'nd', 3: 'rd'}
    IS_POST = isinstance(post, praw.models.Submission)
    DO_TEXT = post.author.name == author or \
              (not IS_POST and post.parent_id is None)
    builder = determineScams(post)
    results = builder.Scams
    replied = False
    if len(results) > 0 and IS_POST:
        TOTAL_CHECKS += 1
    if len(results) > 0:
        doSkip = False
        for scam, confidence in results.items():
            if scam.Name not in HISTORY:
                HISTORY[scam.Name] = 0
            HISTORY[scam.Name] += 1
            if scam.Name == "IgnorePost":
                doSkip = True
            print(scam.Name, confidence)
        if IS_POST:
            HISTORY_TOTAL += 1
        if 10 <= HISTORY_TOTAL % 100 <= 20:
            suffix = 'th'
        else:
            suffix = SUFFIXES.get(HISTORY_TOTAL % 10, 'th')
        TEMPLATE = TEMPLATES[scam.Template]
        built = TEMPLATE.format(TOTAL_CHECKS, str(HISTORY_TOTAL) + suffix)
        if DO_TEXT:
            built += "\r\n - - -"
            if doSkip:
                built += "Detected words indicating I should ignore this post, possibly legit.  "
            built += "\r\nAfter character recognition, text I saw was:\r\n\r\n{0}\r\n".format(builder.FormattedText)
            post.reply(built)
            replied = True
        elif IS_POST and (os.name != "nt" or subReddit.display_name == "mlapi"):
            if not doSkip:
                post.reply(built)
            replied = True
            webHook.sendSubmission(post, builder.ScamText)
            logging.info("Replied to: " + post.title)
    if IS_POST:
        save_history()
    else:
        if builder is None:
            post.reply("Sorry, I was unable to find any image ocr_urls to examine.")
        elif not replied:
            post.reply("No scams detected; text I saw was:\r\n\r\n{0}\r\n".format(builder.FormattedText))
    return builder

_fromcoded = {"guild": {"features": ["DISCOVERABLE", "FROM_HARDCODE"]}}
def getHardCoded(code: str):
    if code == "discord-testers":
        return _fromcoded
    return None

def getInviteData(code: str):
    hardcode = getHardCoded(code)
    if hardcode is not None:
        return hardcode
    url = "https://discord.com/api/v8/invites/" + code
    print("Fetching " + url)
    try:
        r = requests_retry_session(retries=5).get(url)
    except Exception as x:
        logging.error('Could not handle url: {0} {1}'.format(url, x.__class__.__name__))
        print(str(x))
        try:
            e = webHook.getEmbed("Error with Discord Invite",
                str(x), url, x.__class__.__name__)
            logging.info(str(e))
            webHook._sendWebhook(e)
        except:
            pass
        return
    if not r.ok:
        logging.error("Url failed")
        return
    return json.loads(r.text)

def handleNewComment(comment: praw.models.Comment):
    discord_codes = extractURLS(comment, discord_invite_pattern)
    print(discord_codes)
    anyIllegal = False
    for code in discord_codes:
        data = getInviteData(code)
        print(data)
        features = data["guild"]["features"]
        if  "DISCOVERABLE" not in features \
        and "PARTNERED" not in features \
        and "VERIFIED" not in features:
            anyIllegal = True
            break
    if anyIllegal:
        logging.info("Reporting " + comment.id)
        comment.report("Self promotion; not verified/partnered/discoverable (auto-detected /u/mlapibot)")
        try:
            url = "https://www.reddit.com/comments/{0}/comment/{1}/".format(comment.submission.id, comment.id)
            e = webHook.getEmbed("Reported Comment",
                comment.body, url, comment.author.name)
            webHook._sendWebhook(e)
        except:
            pass

def loopPosts():
    for post in subReddit.new(limit=25):
        if post.name in latest_done:
            break # Since we go new -> old, don't go any further into old
        logging.info("Post new: " + post.title)
        saveLatest(post.name)
        handlePost(post)

def loopComments():
    for comment in subReddit.comments(limit=25):
        if comment.id in latest_done:
            break
        logging.info("Comment new: " + comment.id + ", in " + comment.link_id)
        saveLatest(comment.id)
        handleNewComment(comment)

def deleteBadHistory():
    for comment in reddit.user.me().comments.new(limit=10):
        if comment.score < 0:
            webHook.sendRemovedComment(comment)
            comment.delete()


load_scams()

def start():
    logLevel = logging.INFO if os.name == "nt" else logging.INFO
    logging.basicConfig(
        level=logLevel,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler("mlapi.log"),
            logging.StreamHandler(sys.stdout)
        ]
    )
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
        try:
            loopPosts()
        except Exception as e:
            logging.error(e, exc_info=1)
            time.sleep(5)
        if not doneOnce:
            logging.info("Checked posts loop")
        try:
            loopComments()
        except Exception as e:
            logging.error(e, exc_info=1)
            time.sleep(5)
        if not doneOnce:
            logging.info("Checked new subreddit comments")
        try:
            loopInbox()
        except Exception as e:
            logging.error(e, exc_info=1)
            time.sleep(5)
        if not doneOnce:
            logging.info("Checked inbox first loop")
        try:
            deleteBadHistory()
        except Exception as e:
            logging.error(e, exc_info=1)
            time.sleep(5)
        if not doneOnce:
            logging.info("Finished loop")
            doneOnce = True


if __name__ == "__main__":
    start()

