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
import logging
import traceback
from collections import deque
from typing import List, Dict, Union

from d3a.models.strategy.external_strategies import ExternalMixin
from d3a.models.strategy.load_hours import LoadHoursStrategy
from d3a.models.strategy.predefined_load import DefinedLoadStrategy
from d3a_interface.constants_limits import ConstSettings
from pendulum import duration


class LoadExternalMixin(ExternalMixin):
    """
    Mixin for enabling an external api for the load strategies.
    Should always be inherited together with a superclass of LoadHoursStrategy.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    @property
    def filtered_bids_next_market(self) -> List[Dict]:
        """Get a representation of each of the device's bids from the next market."""

        return [
            {'id': bid.id, 'price': bid.price, 'energy': bid.energy}
            for _, bid in self.next_market.get_bids().items()
            if bid.buyer == self.device.name]

    @property
    def _device_info_dict(self):
        return {
            'energy_requirement_kWh':
                self.state.get_energy_requirement_Wh(self.next_market.time_slot) / 1000.0,
            'energy_active_in_bids': self.posted_bid_energy(self.next_market.id),
            'energy_traded': self.energy_traded(self.next_market.id),
            'total_cost': self.energy_traded_costs(self.next_market.id),
        }

    def event_market_cycle(self):
        self._reject_all_pending_requests()
        self.register_on_market_cycle()
        if self.should_use_default_strategy:
            super().event_market_cycle()
        else:
            self.add_entry_in_hrs_per_day()
            self._calculate_active_markets()
            self._update_energy_requirement_future_markets()
            self._delete_past_state()

    def _area_reconfigure_prices(self, **kwargs):
        if self.should_use_default_strategy:
            super()._area_reconfigure_prices(**kwargs)

    def event_tick(self):
        if self.should_use_default_strategy:
            super().event_tick()
        else:
            self._dispatch_event_tick_to_external_agent()

    def event_offer(self, *, market_id, offer):
        if self.should_use_default_strategy:
            super().event_offer(market_id=market_id, offer=offer)

    def _update_bid_aggregator(self, arguments):
        assert set(arguments.keys()) == {'price', 'energy', 'type', 'transaction_id'}
        bid_rate = arguments["price"] / arguments["energy"]
        if bid_rate < 0.0:
            return {
                "command": "update_bid", "status": "error",
                "area_uuid": self.device.uuid,
                "error_message": "Updated bid needs to have a positive price.",
                "transaction_id": arguments.get("transaction_id", None)}
        with self.lock:
            existing_bids = list(self.get_posted_bids(self.next_market))
            existing_bid_energy = sum([bid.energy for bid in existing_bids])
            for bid in existing_bids:
                assert bid.buyer == self.owner.name
                if bid.id in self.next_market.bids.keys():
                    bid = self.next_market.bids[bid.id]
                self.next_market.delete_bid(bid.id)

                self.remove_bid_from_pending(self.next_market.id, bid.id)
            if len(existing_bids) > 0:
                updated_bid = self.post_bid(self.next_market, bid_rate * existing_bid_energy,
                                            existing_bid_energy)
                return {
                    "command": "update_bid", "status": "ready",
                    "bid": updated_bid.to_JSON_string(),
                    "area_uuid": self.device.uuid,
                    "transaction_id": arguments.get("transaction_id", None)}
            else:
                return {
                    "command": "update_bid", "status": "error",
                    "area_uuid": self.device.uuid,
                    "error_message": "Updated bid would only work if the old exist in market.",
                    "transaction_id": arguments.get("transaction_id", None)}

    def _bid_aggregator(self, arguments):
        required_args = {'price', 'energy', 'type', 'transaction_id'}
        allowed_args = required_args.union({'replace_existing'})

        try:
            # Check that all required arguments have been provided
            assert all(arg in arguments.keys() for arg in required_args)
            # Check that every provided argument is allowed
            assert all(arg in allowed_args for arg in arguments.keys())

            replace_existing = arguments.get('replace_existing', True)
            assert self.can_bid_be_posted(
                arguments["energy"],
                arguments["price"],
                self.state.get_energy_requirement_Wh(self.next_market.time_slot) / 1000.0,
                self.next_market,
                replace_existing=replace_existing)

            bid = self.post_bid(
                self.next_market,
                arguments["price"],
                arguments["energy"],
                replace_existing=replace_existing)
            return {
                "command": "bid", "status": "ready",
                "bid": bid.to_JSON_string(replace_existing=replace_existing),
                "area_uuid": self.device.uuid,
                "transaction_id": arguments.get("transaction_id", None)}
        except Exception as e:
            logging.error(f"Error when handling bid on area {self.device.name}: "
                          f"Exception: {str(e)}. Traceback {traceback.format_exc()}")
            return {
                "command": "bid", "status": "error",
                "area_uuid": self.device.uuid,
                "error_message": f"Error when handling bid create "
                                 f"on area {self.device.name} with arguments {arguments}.",
                "transaction_id": arguments.get("transaction_id", None)}

    def _delete_bid_aggregator(self, arguments):
        try:
            to_delete_bid_id = arguments["bid"] if "bid" in arguments else None
            deleted_bids = \
                self.remove_bid_from_pending(self.next_market.id, bid_id=to_delete_bid_id)
            return {
                "command": "bid_delete", "status": "ready", "deleted_bids": deleted_bids,
                "area_uuid": self.device.uuid,
                "transaction_id": arguments.get("transaction_id", None)}
        except Exception as e:
            logging.error(f"Error when handling delete bid on area {self.device.name}: "
                          f"Exception: {str(e)}")
            return {
                "command": "bid_delete", "status": "error",
                "area_uuid": self.device.uuid,
                "error_message": f"Error when handling bid delete "
                                 f"on area {self.device.name} with arguments {arguments}. "
                                 f"Bid does not exist on the current market.",
                "transaction_id": arguments.get("transaction_id", None)}

    def _list_bids_aggregator(self, arguments):
        try:
            return {
                "command": "list_bids", "status": "ready",
                "bid_list": self.filtered_bids_next_market,
                "area_uuid": self.device.uuid,
                "transaction_id": arguments.get("transaction_id", None)}
        except Exception as e:
            logging.error(f"Error when handling list bids on area {self.device.name}: "
                          f"Exception: {str(e)}")
            return {
                "command": "list_bids", "status": "error",
                "area_uuid": self.device.uuid,
                "error_message": f"Error when listing bids on area {self.device.name}.",
                "transaction_id": arguments.get("transaction_id", None)}


class LoadHoursExternalStrategy(LoadExternalMixin, LoadHoursStrategy):
    pass


class LoadProfileExternalStrategy(LoadExternalMixin, DefinedLoadStrategy):
    pass


class LoadForecastExternalStrategy(LoadProfileExternalStrategy):
    """
        Strategy responsible for reading single forecast consumption data via hardware API
    """
    parameters = ('energy_forecast_Wh', 'fit_to_limit', 'energy_rate_increase_per_update',
                  'update_interval', 'initial_buying_rate', 'final_buying_rate',
                  'balancing_energy_ratio', 'use_market_maker_rate')

    def __init__(self, energy_forecast_Wh: float = 0,
                 fit_to_limit=True, energy_rate_increase_per_update=None,
                 update_interval=None,
                 initial_buying_rate: Union[float, dict, str] =
                 ConstSettings.LoadSettings.BUYING_RATE_RANGE.initial,
                 final_buying_rate: Union[float, dict, str] =
                 ConstSettings.LoadSettings.BUYING_RATE_RANGE.final,
                 balancing_energy_ratio: tuple =
                 (ConstSettings.BalancingSettings.OFFER_DEMAND_RATIO,
                  ConstSettings.BalancingSettings.OFFER_SUPPLY_RATIO),
                 use_market_maker_rate: bool = False):
        """
        Constructor of LoadForecastStrategy
        :param energy_forecast_Wh: forecast for the next market slot
        """
        if update_interval is None:
            update_interval = \
                duration(minutes=ConstSettings.GeneralSettings.DEFAULT_UPDATE_INTERVAL)

        super().__init__(daily_load_profile=None,
                         fit_to_limit=fit_to_limit,
                         energy_rate_increase_per_update=energy_rate_increase_per_update,
                         update_interval=update_interval,
                         final_buying_rate=final_buying_rate,
                         initial_buying_rate=initial_buying_rate,
                         balancing_energy_ratio=balancing_energy_ratio,
                         use_market_maker_rate=use_market_maker_rate)

        self.energy_forecast_buffer_Wh = energy_forecast_Wh

    @property
    def channel_dict(self):
        return {**super().channel_dict,
                f'{self.channel_prefix}/set_energy_forecast': self._set_energy_forecast}

    def event_tick(self):
        # Need to repeat he pending request parsing in order to handle power forecasts
        # from the MQTT subscriber (non-connected admin)
        for req in self.pending_requests:
            if req.request_type == "set_energy_forecast":
                self._set_energy_forecast_impl(req.arguments, req.response_channel)

        self.pending_requests = deque(
            req for req in self.pending_requests
            if req.request_type not in "set_energy_forecast")

        super().event_tick()

    def _incoming_commands_callback_selection(self, req):
        if req.request_type == "set_energy_forecast":
            self._set_energy_forecast_impl(req.arguments, req.response_channel)

    def event_market_cycle(self):
        self.update_energy_forecast()
        super().event_market_cycle()

    def event_activate_energy(self):
        self.update_energy_forecast()

    def update_energy_forecast(self):
        # sets energy forecast for next_market
        energy_forecast_Wh = self.energy_forecast_buffer_Wh
        slot_time = self.area.next_market.time_slot
        self.state.set_desired_energy(energy_forecast_Wh, slot_time, overwrite=True)

    def _update_energy_requirement_future_markets(self):
        """
        Setting demanded energy for the next slot is already done by update_energy_forecast
        """
        pass
