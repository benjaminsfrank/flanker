# coding:utf-8

"""
Public interface for flanker address (email or url) parsing and validation
capabilities.

Public Functions in flanker.addresslib.address module:

    * parse(address, addr_spec_only=False)

      Parse a single address or URL. Can parse just the address spec or the
      full mailbox.

    * parse_list(address_list, strict=False, as_tuple=False)

      Parse a list of addresses, operates in strict or relaxed modes. Strict
      mode will fail at the first instance of invalid grammar, relaxed modes
      tries to recover and continue.

    * validate_address(addr_spec)

      Validates (parse, plus dns, mx check, and custom grammar) a single
      address spec. In the case of a valid address returns an EmailAddress
      object, otherwise returns None.

    * validate_list(addr_list, as_tuple=False)

      Validates an address list, and returns a tuple of parsed and unparsed
      portions.

When valid addresses are returned, they are returned as an instance of either
EmailAddress or UrlAddress in flanker.addresslib.address.

See the parser.py module for implementation details of the parser.
"""

import time
import flanker.addresslib.parser
import flanker.addresslib.lexer
import ply.lex
import ply.yacc
from flanker.addresslib.quote import smart_unquote, smart_quote
import flanker.addresslib.validate
import logging

from flanker.utils import is_pure_ascii
from flanker.utils import metrics_wrapper
from flanker.mime.message.headers.encoding import encode_string
from flanker.mime.message.headers.encodedword import mime_to_unicode
from urlparse import urlparse

log = logging.getLogger(__name__)

MAX_ADDRESS_LENGTH = 1024
MAX_ADDRESS_NUMBER = 1024
MAX_ADDRESS_LIST_LENGTH = MAX_ADDRESS_LENGTH * MAX_ADDRESS_NUMBER

@metrics_wrapper()
def parse(address, addr_spec_only=False, metrics=False):
    """
    Given a string, returns a scalar object representing a single full
    mailbox (display name and addr-spec), addr-spec, or a url.

    Returns an Address object and optionally metrics on processing
    time if requested.

    Examples:
        >>> address.parse('John Smith <john@smith.com')
        John Smith <john@smith.com>

        >>> print address.parse('John <john@smith.com>', addr_spec_only=True)
        None

        >>> print address.parse('john@smith.com', addr_spec_only=True)
        'john@smith.com'

        >>> address.parse('http://host.com/post?q')
        http://host.com/post?q

        >>> print address.parse('foo')
        None
    """
    mtimes = {'parsing': 0}
    lexer = flanker.addresslib.lexer.lexer.clone()
    if addr_spec_only:
        parser = flanker.addresslib.parser.addr_spec_parser
    else:
        parser = flanker.addresslib.parser.mailbox_or_url_parser

    # normalize inputs to bytestrings
    if isinstance(address, unicode):
        address = address.encode('utf-8')

    # sanity checks
    if not address:
        return None, mtimes
    if len(address) > MAX_ADDRESS_LENGTH:
        log.warning('address exceeds maximum length of %s', MAX_ADDRESS_LENGTH)
        return None, mtimes

    try:
        bstart = time.time()
        retval = _lift_parser_result(parser.parse(address, lexer=lexer))
        mtimes['parsing'] = time.time() - bstart
    except ply.lex.LexError as e:
        log.warning(u'error in lexing: %s', e)
        return None, mtimes
    except ply.yacc.YaccError as e:
        log.warning(u'error in parsing: %s', e)
        return None, mtimes
    except SyntaxError as e:
        log.warning(u'error in parsing: %s', e)
        return None, mtimes

    return retval, mtimes


@metrics_wrapper()
def parse_discrete_list(address_list, metrics=False):
    """
    Given an string, returns an AddressList object (an iterable list
    representing parsed email addresses and urls).

    Returns an AddressList object and optionally metrics on processing
    time if requested.

    Examples:
        >>> address.parse_list('A <a@b>')
        [A <a@b>]

        >>> address.parse_list('A <a@b>, C <d@e>')
        [A <a@b>, C <d@e>]

        >>> address.parse_list('A <a@b>, C, D <d@e>')
        None

        >>> address.parse_list('A <a@b>, D <d@e>, http://localhost')
        [A <a@b>, D <d@e>, http://localhost]
    """
    mtimes = {'parsing': 0}
    lexer = flanker.addresslib.lexer.lexer.clone()
    parser = flanker.addresslib.parser.mailbox_or_url_list_parser

    # normalize inputs to bytestrings
    if isinstance(address_list, unicode):
        address_list = address_list.encode('utf-8')

    # sanity checks
    if not address_list:
        return None, mtimes
    elif len(address_list) > MAX_ADDRESS_LIST_LENGTH:
        log.warning('address list exceeds maximum length of %s', MAX_ADDRESS_LIST_LENGTH)
        return None, mtimes

    try:
        bstart = time.time()
        retval = _lift_parser_result(parser.parse(address_list, lexer=lexer))
        mtimes['parsing'] = time.time() - bstart
    except ply.lex.LexError as e:
        log.warning(u'error in lexing: %s', e)
        return None, mtimes
    except ply.yacc.YaccError as e:
        log.warning(u'error in parsing: %s', e)
        return None, mtimes
    except SyntaxError as e:
        log.warning(u'error in parsing: %s', e)
        return None, mtimes

    return retval, mtimes

@metrics_wrapper()
def parse_list(address_list, strict=False, as_tuple=False, metrics=False):
    """
    Given an string or list of email addresses and/or urls seperated by a
    delimiter (comma (,) or semi-colon (;)), returns an AddressList object
    (an iterable list representing parsed email addresses and urls).

    The parser can return a list of parsed addresses or a tuple containing
    the parsed and unparsed portions. The parser also returns the parsing
    time metrics if requested.

    Examples:
        >>> address.parse_list('A <a@b>')
        [A <a@b>]

        >>> address.parse_list('A <a@b>, C <d@e>')
        [A <a@b>, C <d@e>]

        >>> address.parse_list('A <a@b>, C, D <d@e>')
        []

        >>> address.parse_list(['A <a@b>', 'C', 'D <d@e>'], as_tuple=True)
        ([A <a@b>, D <d@e>], ['C'])

        >>> address.parse_list('A <a@b>, D <d@e>, http://localhost')
        [A <a@b>, D <d@e>, http://localhost]
    """
    if strict:
        log.warning('strict parsing has been removed, ignoring')

    mtimes = {'parsing': 0}

    if not address_list:
        parsed, unparsed = AddressList(), []
    elif isinstance(address_list, list) and len(address_list) > MAX_ADDRESS_NUMBER:
        log.warning('address list exceeds maximum items of %s', MAX_ADDRESS_NUMBER)
        parsed, unparsed = AddressList(), [address_list]
    elif isinstance(address_list, list):
        parsed, unparsed = AddressList(), []
        for address in address_list:
            if isinstance(address, basestring):
                retval, metrics = parse(address, metrics=True)
                mtimes['parsing'] += metrics['parsing']
                if retval:
                    parsed.append(retval)
                else:
                    unparsed.append(address)
            elif isinstance(address, EmailAddress):
                parsed.append(address)
            elif isinstance(address, UrlAddress):
                parsed.append(address)
            else:
                log.warning('couldnt attempt to parse address list item')
                unparsed.append(address)
    elif isinstance(address_list, basestring) and len(address_list) > MAX_ADDRESS_LIST_LENGTH:
        log.warning('address list exceeds maximum length of %s', MAX_ADDRESS_LIST_LENGTH)
        parsed, unparsed = AddressList(), [address_list]
    elif isinstance(address_list, basestring):
        retval, metrics = parse_discrete_list(address_list, metrics=True)
        mtimes['parsing'] += metrics['parsing']
        if retval:
            parsed, unparsed = retval, []
        else:
            parsed, unparsed = AddressList(), [address_list]
    else:
        log.warning('couldnt attempt to parse address list')
        parsed, unparsed = AddressList(), [address_list]

    if as_tuple:
        return parsed, unparsed, mtimes
    return parsed, mtimes


@metrics_wrapper()
def validate_address(addr_spec, metrics=False):
    """
    Given an addr-spec, runs the pre-parser, the parser, DNS MX checks,
    MX existence checks, and if available, ESP specific grammar for the
    local part.

    In the case of a valid address returns an EmailAddress object, otherwise
    returns None. If requested, will also return the parsing time metrics.

    Examples:
        >>> address.validate_address('john@non-existent-domain.com')
        None

        >>> address.validate_address('user@gmail.com')
        None

        >>> address.validate_address('user.1234@gmail.com')
        user.1234@gmail.com
    """
    mtimes = {'parsing': 0, 'mx_lookup': 0,
        'dns_lookup': 0, 'mx_conn':0 , 'custom_grammar':0}

    # sanity check
    if addr_spec is None:
        return None, mtimes

    # preparse address into its parts and perform any ESP specific pre-parsing
    addr_parts = flanker.addresslib.validate.preparse_address(addr_spec)
    if addr_parts is None:
        return None, mtimes

    # run parser against address
    bstart = time.time()
    paddr = parse('@'.join(addr_parts), addr_spec_only=True)
    mtimes['parsing'] = time.time() - bstart
    if paddr is None:
        return None, mtimes

    # lookup if this domain has a mail exchanger
    exchanger, mx_metrics = \
        flanker.addresslib.validate.mail_exchanger_lookup(addr_parts[-1], metrics=True)
    mtimes['mx_lookup'] = mx_metrics['mx_lookup']
    mtimes['dns_lookup'] = mx_metrics['dns_lookup']
    mtimes['mx_conn'] = mx_metrics['mx_conn']
    if exchanger is None:
        return None, mtimes

    # lookup custom local-part grammar if it exists
    bstart = time.time()
    plugin = flanker.addresslib.validate.plugin_for_esp(exchanger)
    mtimes['custom_grammar'] = time.time() - bstart
    if plugin and plugin.validate(addr_parts[0]) is False:
        return None, mtimes

    return paddr, mtimes


@metrics_wrapper()
def validate_list(addr_list, as_tuple=False, metrics=False):
    """
    Validates an address list, and returns a tuple of parsed and unparsed
    portions.

    Returns results as a list or tuple consisting of the parsed addresses
    and unparsable protions. If requested, will also return parisng time
    metrics.

    Examples:
        >>> address.validate_address_list('a@mailgun.com, c@mailgun.com')
        [a@mailgun.com, c@mailgun.com]

        >>> address.validate_address_list('a@mailgun.com, b@example.com')
        [a@mailgun.com]

        >>> address.validate_address_list('a@b, c@d, e@example.com', as_tuple=True)
        ([a@mailgun.com, c@mailgun.com], ['e@example.com'])
    """
    mtimes = {'parsing': 0, 'mx_lookup': 0,
        'dns_lookup': 0, 'mx_conn':0 , 'custom_grammar':0}

    if not addr_list:
        return AddressList(), mtimes

    # parse addresses
    bstart = time.time()
    parsed_addresses, unparseable = parse_list(addr_list, as_tuple=True)
    mtimes['parsing'] = time.time() - bstart

    plist = AddressList()
    ulist = []

    # make sure parsed list pass dns and esp grammar
    for paddr in parsed_addresses:

        # lookup if this domain has a mail exchanger
        exchanger, mx_metrics = \
            flanker.addresslib.validate.mail_exchanger_lookup(paddr.domain, metrics=True)
        mtimes['mx_lookup'] += mx_metrics['mx_lookup']
        mtimes['dns_lookup'] += mx_metrics['dns_lookup']
        mtimes['mx_conn'] += mx_metrics['mx_conn']

        if exchanger is None:
            ulist.append(paddr.full_spec())
            continue

        # lookup custom local-part grammar if it exists
        plugin = flanker.addresslib.validate.plugin_for_esp(exchanger)
        bstart = time.time()
        if plugin and plugin.validate(paddr.local_part) is False:
            ulist.append(paddr.full_spec())
            continue
        mtimes['custom_grammar'] = time.time() - bstart

        plist.append(paddr)

    # loop over unparsable list and check if any can be fixed with
    # preparsing cleanup and if so, run full validator
    for unpar in unparseable:
        paddr, metrics = validate_address(unpar, metrics=True)
        if paddr:
            plist.append(paddr)
        else:
            ulist.append(unpar)

        # update all the metrics
        for k, v in metrics.iteritems():
            metrics[k] += v

    if as_tuple:
        return plist, ulist, mtimes
    return plist, mtimes


def is_email(string):
    if parse(string, True):
        return True
    return False


class Address(object):
    """
    Base class that represents an address (email or URL). Use it to create
    concrete instances of different addresses:
    """

    @property
    def supports_routing(self):
        """
        Indicates that by default this address cannot be routed.
        """
        return False

    class Type(object):
        """
        Enumerates the types of addresses we support:
            >>> parse('foo@example.com').addr_type
            'email'

            >>> parse('http://example.com').addr_type
            'url'
        """
        Email = 'email'
        Url   = 'url'


class EmailAddress(Address):
    """
    Represents a fully parsed email address with built-in support for MIME
    encoding. Note, do not use EmailAddress class directly, use the parse()
    or parse_list() functions to return a scalar or iterable list respectively.

    Examples:
       >>> addr = EmailAddress("Bob Silva", "bob@host.com")
       >>> addr.address
       'bob@host.com'
       >>> addr.hostname
       'host.com'
       >>> addr.mailbox
       'bob'

    Display name is always returned in Unicode, i.e. ready to be displayed on
    web forms:

       >>> addr.display_name
       u'Bob Silva'

    And full email spec is 100% ASCII, encoded for MIME:
       >>> addr.full_spec()
       'Bob Silva <bob@host.com>'
    """

    _display_name = None
    _local_part = None
    _domain = None
    _addr_type = Address.Type.Email

    def __init__(self, raw_display_name=None, raw_addr_spec=None, display_name=None, local_part=None, domain=None):

        if raw_display_name and raw_addr_spec:
            if isinstance(raw_addr_spec, unicode):
                raw_addr_spec = raw_addr_spec.encode('utf-8')

            lexer = flanker.addresslib.lexer.lexer.clone()
            parser = flanker.addresslib.parser.addr_spec_parser
            mailbox = parser.parse(raw_addr_spec, lexer=lexer)

            self._display_name = raw_display_name
            self._local_part = mailbox.local_part.decode('utf-8')
            self._domain = mailbox.domain.decode('utf-8')

            if self._display_name.startswith('"') and self._display_name.endswith('"') and self._display_name != '""':
                self._display_name = smart_unquote(self._display_name)

        elif raw_display_name:
            if isinstance(raw_display_name, unicode):
                raw_display_name = raw_display_name.encode('utf-8')

            lexer = flanker.addresslib.lexer.lexer.clone()
            parser = flanker.addresslib.parser.mailbox_parser
            mailbox = parser.parse(raw_display_name, lexer=lexer)

            self._display_name = mailbox.display_name.decode('utf-8')
            self._local_part = mailbox.local_part.decode('utf-8')
            self._domain = mailbox.domain.decode('utf-8')

            if self._display_name.startswith('"') and self._display_name.endswith('"') and self._display_name != '""':
                self._display_name = smart_unquote(self._display_name)

        elif raw_addr_spec:
            if isinstance(raw_addr_spec, unicode):
                raw_addr_spec = raw_addr_spec.encode('utf-8')

            lexer = flanker.addresslib.lexer.lexer.clone()
            parser = flanker.addresslib.parser.addr_spec_parser
            mailbox = parser.parse(raw_addr_spec, lexer=lexer)

            self._display_name = ''
            self._local_part = mailbox.local_part.decode('utf-8')
            self._domain = mailbox.domain.decode('utf-8')

        elif local_part and domain:
            self._display_name = display_name or ''
            self._local_part = local_part
            self._domain = domain

        else:
            raise SyntaxError('failed to create EmailAddress: bad parameters')

    @property
    def display_name(self):
        return self._display_name

    @display_name.setter
    def display_name(self, display_name):
        self._display_name = display_name

    @property
    def local_part(self):
        return self._local_part

    @property
    def domain(self):
        return self._domain

    @property
    def addr_type(self):
        return self._addr_type

    @property
    def address(self):
        return u'{}@{}'.format(self.local_part, self.domain.lower())

    @property
    def mailbox(self):
        log.warning('deprecation notice: `mailbox` as been renamed `local_part` to match the nomenclature in RFC 5322 and will be removed in a future version')
        return self._local_part

    @property
    def hostname(self):
        log.warning('deprecation notice: `hostname` as been renamed `domain` to match the nomenclature in RFC 5322 and will be removed in a future version')
        return self._domain.lower()

    def __repr__(self):
        """
        >>> repr(EmailAddress("John Smith", "john@smith.com"))
        'John Smith <john@smith.com>'
        """
        return self.full_spec()

    def __str__(self):
        """
        >>> str(EmailAddress("boo@host.com"))
        'boo@host.com'
        """
        return self.address

    @property
    def supports_routing(self):
        """
        Email addresses can be routed.
        """
        return True

    def full_spec(self):
        """
        Returns a normalized email address as a bytestring. ASCII-compatable
        encoding will be returned if possible. If an ASCII-compatable encoding
        is not possible then any non-ASCII characters will be unchanged.

           >>> EmailAddress("Ev K", "ev@example.com").full_spec()
           'Ev K <ev@example.com>'
           >>> EmailAddress("Жека", "ev@example.com").full_spec()
           '=?utf-8?b?0JbQtdC60LA=?= <ev@example.com>'
        """
        if self.requires_non_ascii():
            return self.to_unicode().encode('utf-8')
        else:
            return self.to_ace().encode('utf-8')

    def to_ace(self):
        """
        Returns an ASCII-compatable encoding of an email address or raises a
        ValueException. Display name and domain parts will be converted to
        ASCII-compatable encoding. The transformed address will be ASCII-only
        and RFC-2822 compliant.
        """
        if not is_pure_ascii(self.local_part):
            raise ValueError('address {} has no ASCII-compatable encoding'.format(self.address.encode('utf-8')))
        ace_domain = self.domain.lower().encode('idna')
        if self.display_name:
            ace_display_name = smart_quote(encode_string(
                None, self.display_name, maxlinelen=MAX_ADDRESS_LENGTH))
            return u'{} <{}@{}>'.format(ace_display_name, self.local_part, ace_domain)
        return u'{}@{}'.format(self.local_part, ace_domain)

    def to_unicode(self):
        """
        Converts to unicode.
        """
        if self.display_name:
            return u'{} <{}@{}>'.format(self.display_name, self.local_part, self.domain)
        return u'{}@{}'.format(self.local_part, self.domain)

    def contains_non_ascii(self):
        '''
        Does the address contain any non-ASCII characters?
        '''
        return not is_pure_ascii(self.address)

    def requires_non_ascii(self):
        '''
        Can the address be converted to an ASCII compatible encoding?
        '''
        return not is_pure_ascii(self.local_part)

    def contains_domain_literal(self):
        '''
        Is the address a domain literal?
        '''
        return self.domain.startswith('[') and self.domain.endswith(']')

    def __cmp__(self, other):
        return True

    def __eq__(self, other):
        """
        Allows comparison of two addresses.
        """
        if isinstance(other, basestring):
            other = parse(other)
        if other:
            return self.address.lower() == other.address.lower()
        return False

    def __ne__(self, other):
        """
        Negative comparison support
        """
        return not (self == other)


    def __hash__(self):
        """
        Hashing allows using Address objects as keys in collections and compare
        them in sets

            >>> a = Address.from_string("a@host")
            >>> b = Address.from_string("A <A@host>")
            >>> hash(a) == hash(b)
            True
            >>> s = set()
            >>> s.add(a)
            >>> s.add(b)
            >>> len(s)
            1
        """
        return hash(self.address.lower())


class UrlAddress(Address):
    """
    Represents a parsed URL:
        >>> url = UrlAddress("http://user@host.com:8080?q=a")
        >>> url.hostname
        'host.com'
        >>> url.port
        8080
        >>> url.scheme
        'http'
        >>> str(url)
        'http://user@host.com:8080?q=a'

    Note: do not create UrlAddress class directly by passing raw "internet
    data", use the parse() and parse_list() functions instead.
    """

    _address = None
    _addr_type = Address.Type.Url

    def __init__(self, raw=None, address=None):

        if raw:
            if isinstance(raw, unicode):
                raw = raw.encode('utf-8')
            lexer = flanker.addresslib.lexer.lexer.clone()
            parser = flanker.addresslib.parser.url_parser
            url = parser.parse(raw, lexer=lexer)
            self._address = url.address.decode('utf-8')
        elif address:
            self._address = address
        else:
            raise SyntaxError('failed to create UrlAddress: bad parameters')

    @property
    def address(self):
        return self._address

    @property
    def addr_type(self):
        return self._addr_type

    @property
    def hostname(self):
        hostname = urlparse(self._address).hostname
        if hostname:
            return hostname.lower()

    @property
    def port(self):
        return urlparse(self._address).port

    @property
    def scheme(self):
        return urlparse(self._address).scheme

    @property
    def path(self):
        return urlparse(self._address).path

    def __str__(self):
        return self.address

    def full_spec(self):
        return self.address

    def to_unicode(self):
        return self.address

    def __repr__(self):
        return self.address

    def __eq__(self, other):
        "Allows comparison of two URLs"
        if isinstance(other, basestring):
            other = parse(other)
        if other:
            return self.address == other.address
        return False

    def __hash__(self):
        return hash(self.address)


class AddressList(object):
    """
    Keeps the list of addresses. Each address is an EmailAddress or
    URLAddress objectAddress-derived object.

    To create a list, use the parse_list method, do not create an
    AddressList directly.

    To see if the address is in the list:
        >>> "missing@host.com" in al
        False
        >>> "bob@host.COM" in al
        True
    """

    container = None

    def __init__(self, container=None):
        if container is None:
            self.container = []
        else:
            self.container = container

    def append(self, n):
        self.container.append(n)

    def remove(self, n):
        self.container.remove(n)

    def __iter__(self):
        return iter(self.container)

    def __getitem__(self, key):
        return self.container[key]

    def __len__(self):
        return len(self.container)

    def __eq__(self, other):
        """
        When comparing ourselves to other lists we must ignore order.
        """
        if isinstance(other, list):
            other = parse_list(other)
        if isinstance(other, basestring):
            other = parse_list(other)
        return set(self.container) == set(other.container)

    def __repr__(self):
        return ''.join(['[', self.full_spec(), ']'])

    def __add__(self, other):
        """
        Adding two AddressLists together yields another AddressList.
        """
        if isinstance(other, list):
            result = self.container + parse_list(other).container
        else:
            result = self.container + other.container
        return AddressList(result)

    def full_spec(self, delimiter=", "):
        """
        Returns a full string which looks pretty much what the original was
        like
            >>> adl = AddressList("Foo <foo@host.com>, Bar <bar@host.com>")
            >>> adl.full_spec(delimiter='; ')
            'Foo <foo@host.com; Bar <bar@host.com>'
        """
        return delimiter.join(addr.full_spec() for addr in self.container)

    def to_unicode(self, delimiter=u", "):
        return delimiter.join(addr.to_unicode() for addr in self.container)

    def to_ascii_list(self):
        return [addr.full_spec() for addr in self.container]

    @property
    def addresses(self):
        """
        Returns a list of just addresses, i.e. no names:
            >>> adl = AddressList("Foo <foo@host.com>, Bar <bar@host.com>")
            >>> adl.addresses
            ['foo@host.com', 'bar@host.com']
        """
        return [addr.address for addr in self.container]

    def __str__(self):
        return self.full_spec()

    @property
    def hostnames(self):
        """
        Returns a set of hostnames used in addresses in this list.
        """
        return set([addr.hostname for addr in self.container])

    @property
    def addr_types(self):
        """
        Returns a set of address types used in addresses in this list.
        """
        return set([addr.addr_type for addr in self.container])


def _lift_parser_result(retval):
    if isinstance(retval, flanker.addresslib.parser.Mailbox):
        return EmailAddress(
            display_name=smart_unquote(retval.display_name.decode('utf-8')),
            local_part=retval.local_part.decode('utf-8'),
            domain=retval.domain.decode('utf-8'))
    if isinstance(retval, flanker.addresslib.parser.Url):
        return UrlAddress(
            address=retval.address.decode('utf-8'))
    if isinstance(retval, list):
        return AddressList(
            map(_lift_parser_result, retval))
    return None
