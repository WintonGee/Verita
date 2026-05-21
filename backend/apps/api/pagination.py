"""
Pagination strategies, chosen per endpoint (see DESIGN.md §6):
  - /v1/invoices  → page+limit  (bounded set, ~12/year; page numbers are useful)
  - /ops/customers → page+limit  (5k max; jump-to-page beats opaque cursors)
  - /v1/usage      → keyset cursor on the time bucket (deep history; avoids the
                     OFFSET penalty). Implemented inline in the usage view since
                     it paginates an aggregation, not a plain queryset.
"""

from rest_framework.pagination import PageNumberPagination


class StandardPageNumberPagination(PageNumberPagination):
    page_size = 25
    max_page_size = 100
    page_size_query_param = "limit"

    def get_paginated_response(self, data):
        from rest_framework.response import Response
        return Response({
            "data": data,
            "page": self.page.number,
            "limit": self.get_page_size(self.request),
            "total": self.page.paginator.count,
        })
