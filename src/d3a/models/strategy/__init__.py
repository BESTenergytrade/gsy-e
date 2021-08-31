"""
Copyright 2018 Grid Singularity
This file is part of D3A.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import json
import sys
from logging import getLogger
from typing import List, Dict, Any, Union, Optional  # noqa
from uuid import uuid4

from d3a import constants
from d3a.constants import FLOATING_POINT_TOLERANCE
from d3a.constants import REDIS_PUBLISH_RESPONSE_TIMEOUT
from d3a.d3a_core.device_registry import DeviceRegistry
from d3a.d3a_core.exceptions import D3ARedisException
from d3a.d3a_core.exceptions import SimulationException, D3AException, MarketException
from d3a.d3a_core.redis_connections.redis_area_market_communicator import BlockingCommunicator
from d3a.d3a_core.util import append_or_create_key
from d3a.events import EventMixin
from d3a.events.event_structures import Trigger, TriggerMixin, AreaEvent, MarketEvent
from d3a.models.base import AreaBehaviorBase
from d3a.models.market import Market
from d3a_interface.dataclasses import (
    Offer, Bid, Trade)
from d3a_interface.constants_limits import ConstSettings

log = getLogger(__name__)


INF_ENERGY = int(sys.maxsize)


class _TradeLookerUpper:
    def __init__(self, owner_name):
        self.owner_name = owner_name

    def __getitem__(self, market):
        for trade in market.trades:
            owner_name = self.owner_name
            if trade.seller == owner_name or trade.buyer == owner_name:
                yield trade


class Offers:
    """
    Keep track of a strategy's accepted and own offers.

    When writing a strategy class, use the post(), remove() and
    replace() methods to keep track of offers.

    posted_in_market() yields all offers that have been posted,
    open_in_market() only those who have not been sold.
    """

    def __init__(self, strategy):
        self.strategy = strategy
        self.bought = {}  # type: Dict[Offer, str]
        self.posted = {}  # type: Dict[Offer, str]
        self.sold = {}  # type: Dict[str, List[Offer]]
        self.split = {}  # type: Dict[str, Offer]

    @property
    def area(self):
        # TODO: Remove the owner and area distinction from the AreaBehaviorBase class
        return self.strategy.area if self.strategy.area is not None else self.strategy.owner

    def _delete_past_offers(self, existing_offers):
        offers = {}
        for offer, market_id in existing_offers.items():
            market = self.area.get_future_market_from_id(market_id)
            if market is not None:
                offers[offer] = market_id
        return offers

    def delete_past_markets_offers(self):
        self.posted = self._delete_past_offers(self.posted)
        self.bought = self._delete_past_offers(self.bought)
        self.split = {}

    @property
    def open(self):
        open_offers = {}
        for offer, market_id in self.posted.items():
            if market_id not in self.sold:
                self.sold[market_id] = []
            if offer not in self.sold[market_id]:
                open_offers[offer] = market_id
        return open_offers

    def bought_offer(self, offer, market_id):
        self.bought[offer] = market_id

    def sold_offer(self, offer, market_id):
        self.sold = append_or_create_key(self.sold, market_id, offer)

    def is_offer_posted(self, market_id, offer_id):
        return offer_id in [offer.id for offer, _market in self.posted.items()
                            if market_id == _market]

    def get_sold_offer_ids_in_market(self, market_id):
        sold_offer_ids = []
        for sold_offer in self.sold.get(market_id, []):
            sold_offer_ids.append(sold_offer.id)
        return sold_offer_ids

    def open_in_market(self, market_id):
        open_offers = []
        sold_offer_ids = self.get_sold_offer_ids_in_market(market_id)
        for offer, _market in self.posted.items():
            if offer.id not in sold_offer_ids and market_id == _market:
                open_offers.append(offer)
        return open_offers

    def open_offer_energy(self, market_id):
        return sum(o.energy for o in self.open_in_market(market_id))

    def posted_in_market(self, market_id):
        return [offer for offer, _market in self.posted.items() if market_id == _market]

    def posted_offer_energy(self, market_id):
        return sum(o.energy for o in self.posted_in_market(market_id))

    def sold_offer_energy(self, market_id):
        return sum(o.energy for o in self.sold_in_market(market_id))

    def sold_offer_price(self, market_id):
        return sum(o.price for o in self.sold_in_market(market_id))

    def can_offer_be_posted(
            self, offer_energy, offer_price, available_energy, market, replace_existing=False):

        if replace_existing:
            # Do not consider previous open offers, since they would be replaced by the current one
            posted_offer_energy = self.sold_offer_energy(market.id)
        else:
            posted_offer_energy = self.posted_offer_energy(market.id)

        posted_energy = (offer_energy
                         + posted_offer_energy
                         - self.sold_offer_energy(market.id))

        return posted_energy <= available_energy and offer_price >= 0.0

    def sold_in_market(self, market_id):
        return self.sold[market_id] if market_id in self.sold else {}

    def bought_in_market(self, market_id):
        return self.bought[market_id] if market_id in self.bought else {}

    def post(self, offer, market_id):
        # If offer was split already, don't post one with the same uuid again
        if offer.id not in self.split:
            self.posted[offer] = market_id

    def remove_offer_from_cache_and_market(self, market, offer_id=None):
        if offer_id is None:
            to_delete_offers = self.open_in_market(market.id)
        else:
            to_delete_offers = [o for o, m in self.posted.items() if o.id == offer_id]
        deleted_offer_ids = []
        for offer in to_delete_offers:
            market.delete_offer(offer.id)
            self.remove(offer)
            deleted_offer_ids.append(offer.id)
        return deleted_offer_ids

    def remove(self, offer):
        try:
            market_id = self.posted.pop(offer)
            assert type(market_id) == str
            if market_id in self.sold and offer in self.sold[market_id]:
                self.strategy.log.warning("Offer already sold, cannot remove it.")
                self.posted[offer] = market_id
            else:
                return True
        except KeyError:
            self.strategy.log.warning("Could not find offer to remove")

    def replace(self, old_offer, new_offer, market_id):
        if self.remove(old_offer):
            self.post(new_offer, market_id)

    def on_trade(self, market_id, trade):
        try:
            if trade.seller == self.strategy.owner.name:
                if trade.offer_bid.id in self.split and trade.offer_bid in self.posted:
                    # remove from posted as it is traded already
                    self.remove(self.split[trade.offer_bid.id])
                self.sold_offer(trade.offer_bid, market_id)
        except AttributeError:
            raise SimulationException("Trade event before strategy was initialized.")

    def on_offer_split(self, original_offer, accepted_offer, residual_offer, market_id):
        if original_offer.seller == self.strategy.owner.name:
            self.split[original_offer.id] = accepted_offer
            self.post(residual_offer, market_id)
            if original_offer in self.posted:
                self.replace(original_offer, accepted_offer, market_id)


class BaseStrategy(TriggerMixin, EventMixin, AreaBehaviorBase):
    available_triggers = [
        Trigger('enable', state_getter=lambda s: s.enabled, help="Enable trading"),
        Trigger('disable', state_getter=lambda s: not s.enabled, help="Disable trading")
    ]

    def __init__(self):
        super(BaseStrategy, self).__init__()
        self.offers = Offers(self)
        self.enabled = True
        self._allowed_disable_events = [AreaEvent.ACTIVATE, MarketEvent.TRADE]
        if ConstSettings.GeneralSettings.EVENT_DISPATCHING_VIA_REDIS:
            self.redis = BlockingCommunicator()
            self.trade_buffer = None
            self.offer_buffer = None
            self.event_response_uuids = []

    parameters = None

    def energy_traded(self, market_id):
        return self.offers.sold_offer_energy(market_id)

    def energy_traded_costs(self, market_id):
        return self.offers.sold_offer_price(market_id)

    def _send_events_to_market(self, event_type_str, market_id, data, callback):
        if not isinstance(market_id, str):
            market_id = market_id.id
        response_channel = f"{market_id}/{event_type_str}/RESPONSE"
        market_channel = f"{market_id}/{event_type_str}"

        data["transaction_uuid"] = str(uuid4())
        self.redis.sub_to_channel(response_channel, callback)
        self.redis.publish(market_channel, json.dumps(data))

        def event_response_was_received_callback():
            return data["transaction_uuid"] in self.event_response_uuids

        self.redis.poll_until_response_received(event_response_was_received_callback)

        if data["transaction_uuid"] not in self.event_response_uuids:
            self.log.error(f"Transaction ID not found after {REDIS_PUBLISH_RESPONSE_TIMEOUT} "
                           f"seconds: {data} {self.owner.name}")
        else:
            self.event_response_uuids.remove(data["transaction_uuid"])

    def area_reconfigure_event(self, *args, **kwargs):
        """Reconfigure the device properties at runtime using the provided arguments.

        This method is triggered when the device strategy is updated while the simulation is
        running. The update can happen via live events (triggered by the user) or scheduled events.
        """

    def event_on_disabled_area(self):
        pass

    def read_config_event(self):
        pass

    def non_attr_parameters(self):
        return dict()

    @property
    def trades(self):
        return _TradeLookerUpper(self.owner.name)

    def energy_balance(self, market, *, allow_open_market=False):
        """
        Return own energy balance for a particular market.

        Negative values indicate bought energy, postive ones sold energy.
        """
        if not allow_open_market and not market.readonly:
            raise ValueError(
                'Energy balance for open market requested and `allow_open_market` no passed')
        return sum(
            t.offer.energy * -1
            if t.buyer == self.owner.name
            else t.offer.energy
            for t in self.trades[market]
        )

    @property
    def is_eligible_for_balancing_market(self):
        if self.owner.name in DeviceRegistry.REGISTRY and \
                ConstSettings.BalancingSettings.ENABLE_BALANCING_MARKET:
            return True

    def offer(self, market_id, offer_args):
        self._send_events_to_market("OFFER", market_id, offer_args,
                                    self._offer_response)
        offer = self.offer_buffer
        assert offer is not None
        self.offer_buffer = None
        return offer

    def post_offer(self, market, replace_existing=True, **offer_kwargs) -> Offer:
        """Post the offer on the specified market.

        Args:
            market: The market in which the offer must be placed.
            replace_existing (bool): if True, delete all previous offers.
            offer_kwargs: the parameters that will be used to create the Offer object.
        """
        if replace_existing:
            # Remove all existing offers that are still open in the market
            self.offers.remove_offer_from_cache_and_market(market)

        offer_kwargs['seller'] = self.owner.name
        offer_kwargs['seller_origin'] = self.owner.name
        offer_kwargs['seller_origin_id'] = self.owner.uuid
        offer_kwargs['seller_id'] = self.owner.uuid

        offer = market.offer(**offer_kwargs)
        self.offers.post(offer, market.id)

        return offer

    def _offer_response(self, payload):
        data = json.loads(payload["data"])
        # TODO: is this additional parsing needed?
        if isinstance(data, str):
            data = json.loads(data)
        if data["status"] == "ready":
            self.offer_buffer = Offer.from_json(data["offer"])
            self.event_response_uuids.append(data["transaction_uuid"])
        else:
            raise D3ARedisException(
                f"Error when receiving response on channel {payload['channel']}:: "
                f"{data['exception']}:  {data['error_message']}")

    def accept_offer(self, market_or_id, offer, *, buyer=None, energy=None,
                     already_tracked=False, trade_rate: float = None,
                     trade_bid_info: float = None, buyer_origin=None,
                     buyer_origin_id=None, buyer_id=None):
        if buyer is None:
            buyer = self.owner.name
        if not isinstance(offer, Offer):
            offer = market_or_id.offers[offer]
        trade = self._accept_offer(market_or_id, offer, buyer, energy, trade_rate, already_tracked,
                                   trade_bid_info, buyer_origin, buyer_origin_id, buyer_id)

        self.offers.bought_offer(trade.offer_bid, market_or_id)
        return trade

    def _accept_offer(self, market_or_id, offer, buyer, energy, trade_rate, already_tracked,
                      trade_bid_info, buyer_origin, buyer_origin_id, buyer_id):

        if ConstSettings.GeneralSettings.EVENT_DISPATCHING_VIA_REDIS:
            if not isinstance(market_or_id, str):
                market_or_id = market_or_id.id
            data = {"offer_or_id": offer.to_json_string(),
                    "buyer": buyer,
                    "energy": energy,
                    "trade_rate": trade_rate,
                    "already_tracked": already_tracked,
                    "trade_bid_info": trade_bid_info if trade_bid_info is not None else None,
                    "buyer_origin": buyer_origin,
                    "buyer_origin_id": buyer_origin_id,
                    "buyer_id": buyer_id}

            self._send_events_to_market("ACCEPT_OFFER", market_or_id, data,
                                        self._accept_offer_response)
            trade = self.trade_buffer
            self.trade_buffer = None
            assert trade is not None
            return trade
        else:
            return market_or_id.accept_offer(offer_or_id=offer, buyer=buyer, energy=energy,
                                             trade_rate=trade_rate,
                                             already_tracked=already_tracked,
                                             trade_bid_info=trade_bid_info,
                                             buyer_origin=buyer_origin,
                                             buyer_origin_id=buyer_origin_id,
                                             buyer_id=buyer_id)

    def _accept_offer_response(self, payload):
        data = json.loads(payload["data"])
        # TODO: is this additional parsing needed?
        if isinstance(data, str):
            data = json.loads(data)
        if data["status"] == "ready":
            self.trade_buffer = Trade.from_json(data["trade"])
            self.event_response_uuids.append(data["transaction_uuid"])
        else:
            raise D3ARedisException(
                f"Error when receiving response on channel {payload['channel']}:: "
                f"{data['exception']}:  {data['error_message']} {data}")

    def post(self, **data):
        self.event_data_received(data)

    def event_data_received(self, data: Dict[str, Any]):
        pass

    def trigger_enable(self, **kw):
        self.enabled = True
        self.log.info("Trading has been enabled")

    def trigger_disable(self):
        self.enabled = False
        self.log.info("Trading has been disabled")
        # We've been disabled - remove all future open offers
        for market in self.area.markets.values():
            for offer in list(market.offers.values()):
                if offer.seller == self.owner.name:
                    self.delete_offer(market, offer)

    def delete_offer(self, market_or_id, offer):
        if ConstSettings.GeneralSettings.EVENT_DISPATCHING_VIA_REDIS:
            data = {"offer_or_id": offer.to_json_string()}
            self._send_events_to_market("DELETE_OFFER", market_or_id, data,
                                        self._delete_offer_response)
        else:
            market_or_id.delete_offer(offer)

    def _delete_offer_response(self, payload):
        data = json.loads(payload["data"])
        # TODO: is this additional parsing needed?
        if isinstance(data, str):
            data = json.loads(data)
        if data["status"] == "ready":
            self.event_response_uuids.append(data["transaction_uuid"])

        else:
            raise D3ARedisException(
                f"Error when receiving response on channel {payload['channel']}:: "
                f"{data['exception']}:  {data['error_message']}")

    def event_listener(self, event_type: Union[AreaEvent, MarketEvent], **kwargs):
        if self.enabled or event_type in self._allowed_disable_events:
            super().event_listener(event_type, **kwargs)

    def event_trade(self, *, market_id, trade):
        """React to offer trades. This method is triggered by the MarketEvent.TRADE event."""
        self.offers.on_trade(market_id, trade)

    def event_offer_split(self, *, market_id, original_offer, accepted_offer, residual_offer):
        self.offers.on_offer_split(original_offer, accepted_offer, residual_offer, market_id)

    def event_market_cycle(self):
        if not constants.RETAIN_PAST_MARKET_STRATEGIES_STATE:
            self.offers.delete_past_markets_offers()
        self.event_responses = []

    def assert_if_trade_offer_price_is_too_low(self, market_id, trade):
        if trade.is_offer_trade and trade.offer_bid.seller == self.owner.name:
            offer = [o for o in self.offers.sold[market_id] if o.id == trade.offer_bid.id][0]
            assert trade.offer_bid.energy_rate >= \
                offer.energy_rate - FLOATING_POINT_TOLERANCE

    def can_offer_be_posted(
            self, offer_energy, offer_price, available_energy, market, replace_existing=False):

        return self.offers.can_offer_be_posted(
            offer_energy, offer_price, available_energy, market, replace_existing=replace_existing)

    def deactivate(self):
        pass

    def event_activate_price(self):
        pass

    @property
    def future_markets_time_slots(self):
        return [m.time_slot for m in self.area.all_markets]

    def get_state(self):
        try:
            return self.state.get_state()
        except AttributeError:
            raise D3AException(
                "Strategy does not have a state. "
                "State is required to support save state functionality.")

    def restore_state(self, saved_state):
        try:
            self.state.restore_state(saved_state)
        except AttributeError:
            raise D3AException(
                "Strategy does not have a state. "
                "State is required to support load state functionality.")

    def update_energy_price(self, market, updated_rate):
        """Update the total price of all offers in the specified market based on their new rate."""
        if market.id not in self.offers.open.values():
            return

        for offer, iterated_market_id in self.offers.open.items():
            iterated_market = self.area.get_future_market_from_id(iterated_market_id)
            # Skip offers that don't belong to the specified market slot
            if market is None or iterated_market is None or iterated_market.id != market.id:
                continue
            try:
                # Delete the old offer and create a new equivalent one with an updated price
                iterated_market.delete_offer(offer.id)
                updated_price = round(offer.energy * updated_rate, 10)
                new_offer = iterated_market.offer(
                    updated_price,
                    offer.energy,
                    self.owner.name,
                    original_offer_price=updated_price,
                    seller_origin=offer.seller_origin,
                    seller_origin_id=offer.seller_origin_id,
                    seller_id=self.owner.uuid
                )
                self.offers.replace(offer, new_offer, iterated_market.id)
            except MarketException:
                continue


class BidEnabledStrategy(BaseStrategy):
    def __init__(self):
        super().__init__()
        self._bids = {}
        self._traded_bids = {}

    def energy_traded(self, market_id):
        offer_energy = super().energy_traded(market_id)
        return offer_energy + self._traded_bid_energy(market_id)

    def energy_traded_costs(self, market_id):
        offer_costs = super().energy_traded_costs(market_id)
        return offer_costs + self._traded_bid_costs(market_id)

    def _remove_existing_bids(self, market: Market) -> None:
        """Remove all existing bids in the market."""

        for bid in self.get_posted_bids(market):
            assert bid.buyer == self.owner.name
            self.remove_bid_from_pending(market.id, bid.id)

    def post_bid(
            self, market: Market, price: float, energy: float, replace_existing: bool = True,
            attributes: Optional[Dict] = None, requirements: Optional[Dict] = None) -> Bid:
        if replace_existing:
            self._remove_existing_bids(market)

        bid = market.bid(
            price,
            energy,
            self.owner.name,
            original_bid_price=price,
            buyer_origin=self.owner.name,
            buyer_origin_id=self.owner.uuid,
            buyer_id=self.owner.uuid,
            attributes=attributes,
            requirements=requirements)
        self.add_bid_to_posted(market.id, bid)
        return bid

    def update_bid_rates(self, market, updated_rate):
        """Replace the rate of all bids in the market slot with the given updated rate."""
        existing_bids = list(self.get_posted_bids(market))
        for bid in existing_bids:
            assert bid.buyer == self.owner.name
            if bid.id in market.bids.keys():
                bid = market.bids[bid.id]
            market.delete_bid(bid.id)

            self.remove_bid_from_pending(market.id, bid.id)
            self.post_bid(market, bid.energy * updated_rate,
                          bid.energy, attributes=bid.attributes, requirements=bid.requirements)

    def can_bid_be_posted(
            self, bid_energy, bid_price, required_energy_kWh, market, replace_existing=False):

        if replace_existing:
            posted_bid_energy = 0.0
        else:
            posted_bid_energy = self.posted_bid_energy(market.id)

        total_posted_energy = (bid_energy + posted_bid_energy)

        return total_posted_energy <= required_energy_kWh and bid_price >= 0.0

    def is_bid_posted(self, market, bid_id):
        return bid_id in [bid.id for bid in self.get_posted_bids(market)]

    def posted_bid_energy(self, market_id):
        if market_id not in self._bids:
            return 0.0
        return sum(b.energy for b in self._bids[market_id])

    def _traded_bid_energy(self, market_id):
        if market_id not in self._traded_bids:
            return 0.0
        return sum(b.energy for b in self._traded_bids[market_id])

    def _traded_bid_costs(self, market_id):
        if market_id not in self._traded_bids:
            return 0.0
        return sum(b.price for b in self._traded_bids[market_id])

    def remove_bid_from_pending(self, market_id, bid_id=None):
        market = self.area.get_future_market_from_id(market_id)
        if market is None:
            return
        if bid_id is None:
            deleted_bid_ids = [bid.id for bid in self.get_posted_bids(market)]
        else:
            deleted_bid_ids = [bid_id]
        for b_id in deleted_bid_ids:
            if b_id in market.bids.keys():
                market.delete_bid(b_id)
        self._bids[market.id] = [bid for bid in self.get_posted_bids(market)
                                 if bid.id not in deleted_bid_ids]
        return deleted_bid_ids

    def add_bid_to_posted(self, market_id, bid):
        if market_id not in self._bids.keys():
            self._bids[market_id] = []
        self._bids[market_id].append(bid)

    def add_bid_to_bought(self, bid, market_id, remove_bid=True):
        if market_id not in self._traded_bids:
            self._traded_bids[market_id] = []
        self._traded_bids[market_id].append(bid)
        if remove_bid:
            self.remove_bid_from_pending(market_id, bid.id)

    def get_traded_bids_from_market(self, market):
        if market.id not in self._traded_bids:
            return []
        else:
            return self._traded_bids[market.id]

    def are_bids_posted(self, market_id):
        """Checks if any bids have been posted in the market slot with the given ID."""
        if market_id not in self._bids:
            return False
        return len(self._bids[market_id]) > 0

    def post_first_bid(self, market, energy_Wh):
        # TODO: It will be safe to remove this check once we remove the event_market_cycle being
        # called twice, but still it is nice to have it here as a precaution. In general, there
        # should be only bid from a device to a market at all times, which will be replaced if
        # it needs to be updated. If this check is not there, the market cycle event will post
        # one bid twice, which actually happens on the very first market slot cycle.
        if not all(bid.buyer != self.owner.name for bid in market.get_bids().values()):
            self.owner.log.warning("There is already another bid posted on the market, therefore"
                                   " do not repost another first bid.")
            return None
        return self.post_bid(
            market,
            energy_Wh * self.bid_update.initial_rate[market.time_slot] / 1000.0,
            energy_Wh / 1000.0,
        )

    def get_posted_bids(self, market):
        if market.id not in self._bids:
            return []
        return self._bids[market.id]

    def event_bid_deleted(self, *, market_id, bid):
        assert ConstSettings.IAASettings.MARKET_TYPE != 1, \
            "Invalid state, cannot receive a bid if single sided market is globally configured."

        if bid.buyer != self.owner.name:
            return
        self.remove_bid_from_pending(market_id, bid.id)

    def event_bid_split(self, *, market_id, original_bid, accepted_bid, residual_bid):
        assert ConstSettings.IAASettings.MARKET_TYPE != 1, \
            "Invalid state, cannot receive a bid if single sided market is globally configured."
        if accepted_bid.buyer != self.owner.name:
            return
        self.add_bid_to_posted(market_id, bid=accepted_bid)
        self.add_bid_to_posted(market_id, bid=residual_bid)

    def event_bid_traded(self, *, market_id, bid_trade):
        """Register a successful bid when a trade is concluded for it.

        This method is triggered by the MarketEvent.BID_TRADED event.
        """
        assert ConstSettings.IAASettings.MARKET_TYPE != 1, \
            "Invalid state, cannot receive a bid if single sided market is globally configured."

        if bid_trade.buyer == self.owner.name:
            self.add_bid_to_bought(bid_trade.offer_bid, market_id)

    def event_market_cycle(self):
        if not constants.RETAIN_PAST_MARKET_STRATEGIES_STATE:
            self._bids = {}
            self._traded_bids = {}
            super().event_market_cycle()

    def assert_if_trade_bid_price_is_too_high(self, market, trade):
        if trade.is_bid_trade and trade.offer_bid.buyer == self.owner.name:
            bid = [b for b in self.get_posted_bids(market) if b.id == trade.offer_bid.id][0]
            assert trade.offer_bid.energy_rate <= bid.energy_rate + FLOATING_POINT_TOLERANCE
