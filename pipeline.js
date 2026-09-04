// Prospect sites we're working on, by stage. SAMPLE data — replace with your real
// pipeline.
//
// Two outreach paths converge at negotiating:
//   mail campaign : postcard_mailed -> responded -> consultation -> negotiating
//   direct (older,
//   still in use) : contacted ----------------------> negotiating
// then: negotiating -> contract_sent -> contract_signed -> shipment -> operational
//
// Postcards have no delivery tracking, so the first mail stage is "mailed", not
// "delivered". QR scan and form-fill are collapsed into "responded" because the
// detail lives in the separate mailing-campaign tracker.
window.PIPELINE = [
  {name: "Sunset Smoke Shop",     address: "7021 Sunset Blvd, Los Angeles, CA 90028",   stage: "postcard_mailed", lat: 34.0980, lng: -118.3430},
  {name: "Inglewood Mini Mart",   address: "600 E Manchester Blvd, Inglewood, CA 90301", stage: "postcard_mailed", lat: 33.9610, lng: -118.3480},
  {name: "Vermont Smoke & Vape",  address: "1200 S Vermont Ave, Los Angeles, CA 90006",  stage: "responded",       lat: 34.0490, lng: -118.2920},
  {name: "Reseda Liquor",         address: "18300 Sherman Way, Reseda, CA 91335",         stage: "contacted",       lat: 34.2010, lng: -118.5330},
  {name: "Gage Ave Market",       address: "2500 E Gage Ave, Huntington Park, CA 90255",  stage: "consultation",    lat: 33.9800, lng: -118.2100},
  {name: "Crenshaw Wash House",   address: "3900 Crenshaw Blvd, Los Angeles, CA 90008",   stage: "negotiating",     lat: 34.0110, lng: -118.3350},
  {name: "Whittier Chevron",      address: "8000 Whittier Blvd, Los Angeles, CA 90022",   stage: "contract_sent",   lat: 34.0225, lng: -118.1560},
  {name: "Alvarado Smoke Shop",   address: "700 S Alvarado St, Los Angeles, CA 90057",    stage: "contract_signed", lat: 34.0570, lng: -118.2790},
  {name: "Pico Blvd Market",      address: "4400 W Pico Blvd, Los Angeles, CA 90019",     stage: "shipment",        lat: 34.0480, lng: -118.3350},
  {name: "Figueroa Smoke & Vape", address: "5400 N Figueroa St, Los Angeles, CA 90042",   stage: "operational",     lat: 34.1120, lng: -118.1930},
];
