import threading
import urllib2
import os
import re

import lazylibrarian

from lazylibrarian import logger, database, formatter, notifiers, providers

from lib.fuzzywuzzy import fuzz
from hashlib import sha1
import lazylibrarian.common as common
from lazylibrarian.searchtorrents import TORDownloadMethod
from lazylibrarian.searchnzb import NZBDownloadMethod


def search_rss_book(books=None, reset=False):
    if not(lazylibrarian.USE_RSS()):
        logger.warn('RSS search is disabled')
        common.schedule_job(action='Stop', target='search_rss_book')
        return
    # rename this thread
    threading.currentThread().name = "SEARCHRSSBOOKS"
    myDB = database.DBConnection()
    searchlist = []

    if books is None:
        # We are performing a backlog search
        searchbooks = myDB.select('SELECT BookID, AuthorName, Bookname, BookAdded from books WHERE Status="Wanted" order by BookAdded desc')
    else:
        # The user has added a new book
        searchbooks = []
        for book in books:
            searchbook = myDB.select('SELECT BookID, AuthorName, BookName from books WHERE BookID="%s" \
                                     AND Status="Wanted"' % book['bookid'])
            for terms in searchbook:
                searchbooks.append(terms)

    if len(searchbooks) == 0:
        logger.debug("RSS search requested for no books or invalid BookID")
        return
    elif len(searchbooks) == 1:
        logger.info('RSS Searching for one book')
    else:
        logger.info('RSS Searching for %i books' % len(searchbooks))

    resultlist, nproviders = providers.IterateOverRSSSites()
    if not nproviders:
        logger.warn('No rss providers are set, check config')
        return  # No point in continuing

    dic = {'...': '', '.': ' ', ' & ': ' ', ' = ': ' ', '?': '', '$': 's', ' + ': ' ', '"': '',
           ',': '', '*': '', ':': '', ';': ''}

    rss_count = 0
    for book in searchbooks:
        bookid = book['BookID']
        author = book['AuthorName']
        title = book['BookName']

        author = formatter.latinToAscii(formatter.replace_all(author, dic))
        title = formatter.latinToAscii(formatter.replace_all(title, dic))

        found = processResultList(resultlist, author, title, book)

        # if you can't find the book, try author without initials,
        # and title without any "(extended details, series etc)"
        if not found:
            if author[1] in '. ' or '(' in title:  # anything to shorten?
                while author[1] in '. ':  # strip any initials
                    author = author[2:].strip()  # and leading whitespace
                if '(' in title:
                    title = title.split('(')[0]
                found = processResultList(resultlist, author, title, book)

        if not found:
            logger.debug("Searches returned no results. Adding book %s - %s to queue." % (author, title))
        else:
            rss_count = rss_count + 1

    if rss_count == 1:
        logger.info("RSS Search for Wanted items complete, found %s book" % rss_count)
    else:
        logger.info("RSS Search for Wanted items complete, found %s books" % rss_count)

    if reset:
        common.schedule_job(action='Restart', target='search_rss_book')


def processResultList(resultlist, author, title, book):
    myDB = database.DBConnection()
    dictrepl = {'...': '', '.': ' ', ' & ': ' ', ' = ': ' ', '?': '', '$': 's', ' + ': ' ', '"': '',
                ',': ' ', '*': '', '(': '', ')': '', '[': '', ']': '', '#': '', '0': '', '1': '', '2': '',
                '3': '', '4': '', '5': '', '6': '', '7': '', '8': '', '9': '', '\'': '', ':': '', '!': '',
                '-': ' ', '\s\s': ' '}
                # ' the ': ' ', ' a ': ' ', ' and ': ' ', ' to ': ' ', ' of ': ' ',
                # ' for ': ' ', ' my ': ' ', ' in ': ' ', ' at ': ' ', ' with ': ' '}

    match_ratio = int(lazylibrarian.MATCH_RATIO)
    reject_list = formatter.getList(lazylibrarian.REJECT_WORDS)

    # bit of a misnomer now, rss can search both tor and nzb rss feeds
    for tor in resultlist:
        tor_Title = formatter.latinToAscii(formatter.replace_all(tor['tor_title'], dictrepl)).strip()
        tor_Title = re.sub(r"\s\s+", " ", tor_Title)  # remove extra whitespace

        tor_Author_match = fuzz.token_set_ratio(author, tor_Title)
        tor_Title_match = fuzz.token_set_ratio(title, tor_Title)
        logger.debug("RSS Author/Title Match: %s/%s for %s" % (tor_Author_match, tor_Title_match, tor_Title))

        rejected = False
        for word in reject_list:
            if word in tor_Title.lower() and not word in author.lower() and not word in book.lower():
                rejected = True
                logger.debug("Rejecting %s, contains %s" % (tor_Title, word))
                break

        if (tor_Title_match >= match_ratio and tor_Author_match >= match_ratio and not rejected):
            logger.debug(u'Found RSS: %s' % tor['tor_title'])
            bookid = book['bookid']
            tor_Title = (book["authorName"] + ' - ' + book['bookName'] +
                         ' LL.(' + book['bookid'] + ')').strip()
            tor_url = tor['tor_url']
            tor_prov = tor['tor_prov']
            tor_feed = tor['tor_feed']

            tor_size_temp = tor['tor_size']  # Need to cater for when this is NONE (Issue 35)
            if tor_size_temp is None:
                tor_size_temp = 1000
            tor_size = str(round(float(tor_size_temp) / 1048576, 2)) + ' MB'
            controlValueDict = {"NZBurl": tor_url}
            newValueDict = {
                "NZBprov": tor_prov,
                "BookID": bookid,
                "NZBdate": formatter.now(),  # when we asked for it
                "NZBsize": tor_size,
                "NZBtitle": tor_Title,
                "NZBmode": "torrent",
                "Status": "Skipped"
            }
            myDB.upsert("wanted", newValueDict, controlValueDict)
            snatchedbooks = myDB.action('SELECT * from books WHERE BookID="%s" and Status="Snatched"' %
                                        bookid).fetchone()
            if not snatchedbooks:  # check if one of the other downloaders got there first
                if '.nzb' in tor_url:
                    snatch = NZBDownloadMethod(bookid, tor_prov, tor_Title, tor_url)
                else:
                    #  http://baconbits.org/torrents.php?action=download&authkey=<authkey>&torrent_pass=<password.hashed>&id=185398
                    if not tor_url.startswith('magnet'):  # magnets don't use auth
                        pwd = lazylibrarian.RSS_PROV[tor_feed]['PASS']
                        auth = lazylibrarian.RSS_PROV[tor_feed]['AUTH']
                        # don't know what form of password hash is required, try sha1
                        tor_url = tor_url.replace('<authkey>', auth).replace('<password.hashed>', sha1(pwd))
                    snatch = TORDownloadMethod(bookid, tor_prov, tor_Title, tor_url)

                if snatch:
                    notifiers.notify_snatch(formatter.latinToAscii(tor_Title) + ' at ' + formatter.now())
                    common.schedule_job(action='Start', target='processDir')
                    return True

    logger.debug("No RSS found for " + (book["authorName"] + ' ' +
                 book['bookName']).strip())
    return False
