from datetime import datetime, timedelta

import pytz
from sqlalchemy import and_, case, func, literal, or_, select, union
from sqlalchemy.orm import joinedload, noload, raiseload
from sqlalchemy.dialects.postgresql import aggregate_order_by

from app import db
from app.api.helpers import Service
from app.models import (CaseStudy,
                        Domain,
                        Framework,
                        Supplier,
                        SupplierDomain,
                        SupplierFramework,
                        User)
import requests
from requests.exceptions import (HTTPError, Timeout, ConnectionError, SSLError, ProxyError)
import re
from app.tasks import publish_tasks
from app.api.business.errors import AbrError
from flask import current_app
import xml.etree.ElementTree as ElementTree
import json
from xml.sax import saxutils


class AbrService(Service):

    def __init__(self, *args, **kwargs):
        super(AbrService, self).__init__(*args, **kwargs)

    # using the supplier's abn to set up the link to be sent ABR API
    def get_url(self, abn):
        api_key = current_app.config['ABR_API_KEY']
        include_historical_details = 'N'
        link = 'https://abr.business.gov.au/abrxmlsearch/AbrXmlSearch.asmx/SearchByABNv201205?searchString='

        url = link + abn + '&includeHistoricalDetails=' + include_historical_details + '&authenticationGuid=' + api_key
        business_info_by_abn = self.fetch_data(url)
        return business_info_by_abn
    
    def fetch_data(self, url):
        try:
            response = requests.get(url)

            if response.ok:
                xmlText = response.content
                xml_parsed_data = self.get_data(xmlText)
                return xml_parsed_data

            response.raise_for_status()

        # Rasing different exceptions
        #timeout is considered as payload exception
        except ConnectionError as ex:
            raise AbrError('Connection Error')

        except HTTPError as ex:
            raise AbrError('HTTP Error')

        except ProxyError as ex:
            raise AbrError('ProxyError')

        except SSLError as ex:
            raise AbrError('SSLError')

        except Exception as ex:
                raise AbrError('Failed exception raised')


    def get_data(self, xmlText):
        print("inside get data function")
        if re.findall(r'<organisationName>(.*?)</organisationName>', xmlText):
            search_xml_organisation_name = re.findall(r'<organisationName>(.*?)</organisationName>', xmlText)
            organisation_name = search_xml_organisation_name[0]
            # this only works for &, < and > but not ' and ""
            organisation_name = saxutils.unescape(organisation_name)

            # takes the first postcode
            search_xml_postcode = re.findall(r'<postcode>(.*?)</postcode>', xmlText)
            postcode = search_xml_postcode[0]

            # takes the first state
            search_xml_state = re.findall(r'<stateCode>(.*?)</stateCode>', xmlText)
            state = search_xml_state[0]
            print("organisation name, postcode and state" + organisation_name + postcode + state)

            return json.dumps({
                'organisation_name': organisation_name,
                'postcode': postcode,
                'state': state
            })
        
        # Payload exceptions: https://abr.business.gov.au/Documentation/Exceptions
        else:
            search_exception_code = re.findall(r'<exceptionCode>(.*?)</exceptionCode>', xmlText)
            exception_code = search_exception_code[0]

            search_exception_description = re.findall(r'<exceptionDescription>(.*?)</exceptionDescription>', xmlText)
            exception_description = search_exception_description[0]

            raise AbrError(exception_code + ': ' + exception_description)
