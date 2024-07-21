# app.py
from dotenv import load_dotenv
import os
from enum import Enum
from services.appleSetup import AppleAuth
from services.apple_maps import apple_maps_service
from services.google_maps import google_maps_service
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from typing import List, Any, Optional, Dict
import appwrite
from appwrite.client import Client
from appwrite.query import Query
from appwrite.services.users import Users
from appwrite.services.databases import Databases
from appwrite.id import ID
from apscheduler.schedulers.background import BackgroundScheduler
from featureFunctions import get_search_region
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime
import googlemaps
from appwrite.exception import AppwriteException
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

# Initialize Appwrite client
client = Client()
client.set_endpoint('https://cloud.appwrite.io/v1')
client.set_project('66930c61001b090ab206')
client.set_key(os.getenv('APPWRITE_API_KEY'))
client.set_self_signed()

# Appwrite configuration
appwrite_config = {
    "database_id": "66930e1000087eb0d4bd",
    "user_collection_id": "66930e5900107bc194dc",
    "preferences_collection_id": "6696016b00117bbf6352",
    "friends_collection_id": "friends",
    "locations_collection_id": "669d2a590010d4bf7d30"


}

# Initialize FastAPI app
app = FastAPI(
    title="Proxi Link AI API",
    description="API for Proxi Link App",
    version="1.0.0",
)


def update_apple_token():
    global global_maps_token
    global_maps_token = AppleAuth.generate_apple_token()
    print(f"Token updated at {datetime.now()}")


# Set up scheduler for token updates
scheduler = BackgroundScheduler()
scheduler.add_job(
    func=update_apple_token,
    trigger=IntervalTrigger(hours=168),
    id='apple_token_update',
    name='Update Apple Token every 7 days',
    replace_existing=True
)
scheduler.start()

if (os.getenv('DEV')):
    global_maps_token = os.getenv('TOKEN_TEMP')
else:
    global_maps_token = None

database = Databases(client)

# Initialize Google Maps client
gmaps = googlemaps.Client(key=os.getenv('GOOGLE_MAPS_API'))


class Location(BaseModel):
    lat: float
    lon: float


class SocialInteraction(str, Enum):
    ENERGETIC = "ENERGETIC"
    RELAXED = "RELAXED"
    BOTH = "BOTH"


class Time(str, Enum):
    MORNING = "MORNING"
    AFTERNOON = "AFTERNOON"
    EVENING = "EVENING"
    NIGHT = "NIGHT"


class Shopping(str, Enum):
    YES = "YES"
    SOMETIME = "SOMETIME"
    NO = "NO"


class Preferences(BaseModel):
    # Temp
    user_id: str
    users: Any
    cuisine: List[str]
    atmosphere: List[str]
    entertainment: List[str]
    socializing: SocialInteraction = SocialInteraction.BOTH
    Time: List[Time]
    shopping: Shopping = Shopping.SOMETIME
    family_friendly: bool
    learning: List[str]
    sports: List[str]


@app.get("/", summary="Root")
async def root():
    return {"message": "Welcome to the Proxi Link API"}


class AddressInput(BaseModel):
    address: str
    name: str


class PlaceDetails(BaseModel):
    address: str
    ID: str
    rating: float
    name: str
    hours: List[str]
    url: str
    photos: List[str]

@app.post("/get_place_details", summary="Get Place Details from the Database or Google Maps")
async def get_place_details(address_input: AddressInput):
    """
    Get place details from the database or Google Maps.

    Payload: {"address": "123 Main St, City, State", "name": "Place Name"}
    From Apple Maps get the address and name and this cooks.
    """
    try:
        address = address_input.address
        name = address_input.name
        logger.info(f"Fetching details for address: {address}")

        # Search for existing entry in the database
        result = database.list_documents(
            database_id=appwrite_config['database_id'],
            collection_id=appwrite_config['locations_collection_id'],
            queries=[Query.equal('address', [address])]
        )

        if result['total'] > 0:
            logger.info("Updating existing details in database")
            existing_document = result['documents'][0]
            place_id = existing_document['$id']
        else:
            logger.info("Creating new document for the location")
            place_id = ID.unique()

        # Fetch details from Google Maps
        details = google_maps_service.find_place(f"{name}, {address}")
        if 'places' in details and len(details['places']) > 0:
            place = details['places'][0]
            place_details = {
                "address": address,
                "ID": place.get('id', '0'),
                "rating": place.get('rating', 0),
                "name": place.get('displayName', name),
                "hours": place.get('currentOpeningHours', {}).get('weekdayDescriptions', []),
                "url": place.get('websiteUri', ''),
                "photos": place.get('photo_urls', [])
            }

            # Update or create the document in the database
            if result['total'] > 0:
                update_response = database.update_document(
                    database_id=appwrite_config['database_id'],
                    collection_id=appwrite_config['locations_collection_id'],
                    document_id=place_id,
                    data=place_details
                )
            else:
                create_response = database.create_document(
                    database_id=appwrite_config['database_id'],
                    collection_id=appwrite_config['locations_collection_id'],
                    document_id=place_id,
                    data=place_details
                )
            return place_details
        else:
            logger.warning("No detailed place information found")
            raise HTTPException(status_code=404, detail="No detailed place information found")

    except Exception as e:
        logger.error(f"Error getting place details: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Error getting place details: {str(e)}")


@app.get("/initial-recommendations")
async def get_initial_recommendations(lat: float, lon: float, user_id: str):
    try:
        # Define proximity margin for location checking
        proximity_margin = 0.05  # Adjust based on desired precision

        # Search for nearby existing entries in the database
        results = database.list_documents(
            database_id=appwrite_config['database_id'],
            collection_id=appwrite_config['locations_collection_id'],
            queries=[
                Query.greater('latitude', lat - proximity_margin),
                Query.less('latitude', lat + proximity_margin),
                Query.greater('longitude', lon - proximity_margin),
                Query.less('longitude', lon + proximity_margin)
            ]
        )

        if results['total'] > 0:
            return {"recommendations": [doc for doc in results['documents']]}
        else:
            categories = ["food", "entertainment", "nature/park"]
            recommendations = []
            access_token = await apple_maps_service.get_access_token()

            async with httpx.AsyncClient() as client:
                for category in categories:
                    response = await client.get(
                        "https://maps-api.apple.com/v1/search",
                        params={
                            "q": category,
                            "searchLocation": f"{lat},{lon}",
                            "lang": "en-US",
                            "limit": 5  # Increase limit to fetch more results
                        },
                        headers={"Authorization": f"Bearer {access_token}"}
                    )
                    if response.status_code == 200:
                        for place in response.json().get('results', []):
                            # Store new place details in the database
                            place_data = {
                                "name": place['name'],
                                "description": category,
                                "latitude": place['coordinate']['latitude'],
                                "longitude": place['coordinate']['longitude'],
                                "address": ", ".join(place.get("formattedAddressLines", [])),
                                "url": place.get('url', '')
                            }
                            create_response = database.create_document(
                                database_id=appwrite_config['database_id'],
                                collection_id=appwrite_config['locations_collection_id'],
                                document_id=ID.unique(),
                                data=place_data
                            )
                            recommendations.append(create_response)
            return {"recommendations": recommendations}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error fetching initial recommendations: {str(e)}"
        )

@app.get("/get-apple-token", summary="Get Apple Maps Token")
async def get_apple_token():
    """
    Retrieve the current Apple Maps token. If not available, generate a new one.
    """
    token = await apple_maps_service.get_access_token()
    print(token)
    if token:
        return {"apple_token generated successfully"}
    else:
        return {"message": "Error generating Apple Maps token"}


class AccountInfo(BaseModel):
    uid: str
    firstName: str
    lastName: str
    address: Optional[str] = None


@app.post("/update-account", summary="Update Account")
async def update_account(account: AccountInfo):
    """
    Update user account in the database
    """
    try:
        account_dict = account.model_dump()
        existing_account = database.get_document(
            database_id=appwrite_config['database_id'],
            collection_id=appwrite_config['user_collection_id'],
            document_id=account_dict['uid']
        )
        if account_dict['address'] is None:
            account_dict['address'] = existing_account['address']

        result = database.update_document(
            database_id=appwrite_config['database_id'],
            collection_id=appwrite_config['user_collection_id'],
            document_id=account_dict['uid'],
            data=account_dict
        )

        return {"message": "User account updated successfully", "document_id": result['$id']}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error updating user account: {str(e)}")


# App .get pereferences by the user_id. Pass in user ID in the body, and we query the document and get it, and return errything as a json. Doc ID is same as User ID FYI
# result = databases.get_document(
#     database_id = appwrite_config['database_id'],
#     collection_id = appwrite_config['preferences_collection_id'],
#     document_id = unique_id
#     queries = [] # optional
# )
@app.get("/get-preferences", summary="Get Preferences")
async def get_preferences(user_id: str):
    """
    Get preferences based on userID
    """
    try:
        result = database.get_document(
            database_id=appwrite_config['database_id'],
            collection_id=appwrite_config['preferences_collection_id'],
            document_id=user_id
        )
        return result
    except AppwriteException as e:
        if e.code == 404:  # Assuming 404 is the status code for "document not found"
            # Return an empty structure if the document is not found
            return {
            }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error getting preferences: {str(e)}")


@app.post("/submit-preferences", summary="Submit User Preferences")
async def submit_preferences(preferences: Preferences):
    """
    Submit and store or update user preferences in the database.
    """
    try:
        preferences_dict = preferences.model_dump()
        unique_id = preferences_dict['user_id']

        # Check if the document exists
        try:
            existing_document = database.get_document(
                database_id=appwrite_config['database_id'],
                collection_id=appwrite_config['preferences_collection_id'],
                document_id=unique_id
            )
            # If the document exists, update it
            result = database.update_document(
                database_id=appwrite_config['database_id'],
                collection_id=appwrite_config['preferences_collection_id'],
                document_id=unique_id,
                data=preferences_dict
            )
        except Exception as e:
            # If the document does not exist, create it
            result = database.create_document(
                database_id=appwrite_config['database_id'],
                collection_id=appwrite_config['preferences_collection_id'],
                document_id=unique_id,
                data=preferences_dict
            )

        return {"message": "Preferences submitted successfully", "document_id": result['$id']}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error submitting preferences: {str(e)}")


# Recommendation using preferences in user collection and location use later
@app.get("/recommendations", summary="Get Recommendations")
async def get_recommendations(user_id: str):
    """
    Generate restaurant recommendations based on user preferences and location.
    """
    # Fetch user data from the database
    user_data = database.get_document(
        database_id=appwrite_config['database_id'],
        collection_id=appwrite_config['user_collection_id'],
        document_id=user_id
    )
    preferences = user_data['preferences']
    location = user_data['location']

    # Use Google Maps API to find places based on preferences and location
    # places = gmaps.places_nearby(
    #     location=location, keyword=preferences, radius=5000)

    # Store recommendations in the database
    # database.create_document(
    #     database_id=appwrite_config['database_id'],
    #     collection_id='recommendations',
    #     document_id=ID.unique(),
    #     data={'places': places['results']}
    # )

    return {"recommendations": places['results']}


# Default recommendations based on only location
@app.post("/get-recommendations", summary="Get Recommendations")
async def get_recommendations(location: Location):
    """
    Return the closest places from Google Places API based on location and category.
    """
    # Default recommendations based on only location
    dummy_recommendations = [
        {"type": "entertainment", "name": "Live Music Venue",
            "location": {"lat": 37.78825, "lon": -122.4324}},
        {"type": "food", "name": "Italian Restaurant",
            "location": {"lat": 37.78925, "lon": -122.4314}},
        {"type": "shopping", "name": "Local Bookstore",
            "location": {"lat": 37.79025, "lon": -122.4304}},
        {"type": "sightseeing", "name": "Golden Gate Park",
            "location": {"lat": 37.79125, "lon": -122.4294}},
        {"type": "cafe", "name": "Cozy Cafe", "location": {
            "lat": 37.79225, "lon": -122.4284}}
    ]

    # Uncomment and use this section when you want to use Google Places API
    # try:
    #     async with httpx.AsyncClient() as client:
    #         recommendations = []
    #         categories = ["music", "entertainment", "food", "museum", "park"]
    #
    #         for category in categories:
    #             response = await client.get(
    #                 "https://maps.googleapis.com/maps/api/place/nearbysearch/json",
    #                 params={
    #                     "location": f"{location.lat},{location.lon}",
    #                     "radius": 5000,  # Radius in meters
    #                     "type": category,
    #                     "key": GOOGLE_API_KEY
    #                 }
    #             )
    #             results = response.json().get("results", [])
    #
    #             # Sort places by distance (by their location)
    #             sorted_places = sorted(
    #                 results,
    #                 key=lambda place: (
    #                     (place["geometry"]["location"]["lat"] - location.lat) ** 2 +
    #                     (place["geometry"]["location"]["lng"] - location.lon) ** 2
    #                 )
    #             )
    #
    #             # Add top 5 closest places to recommendations
    #             for place in sorted_places[:5]:
    #                 recommendations.append({
    #                     "type": category,
    #                     "name": place.get("name"),
    #                     "location": {
    #                         "lat": place.get("geometry", {}).get("location", {}).get("lat"),
    #                         "lon": place.get("geometry", {}).get("location", {}).get("lng")
    #                     }
    #                 })
    #
    #         return {"recommendations": recommendations}
    # except httpx.HTTPStatusError as e:
    #     raise HTTPException(status_code=e.response.status_code, detail=f"Google Maps API error: {str(e)}")
    # except httpx.RequestError as e:
    #     raise HTTPException(status_code=500, detail=f"Request error: {str(e)}")

    return {"recommendations": dummy_recommendations}


class Location(BaseModel):
    lat: float
    lon: float


class ProximityRecommendationRequest(BaseModel):
    locations: List[Location]
    interests: List[str]


def calculate_centroid(locations: List[Location]) -> Dict[str, float]:
    """
    Calculate the centroid of given locations.
    """
    latitudes = [loc.lat for loc in locations]
    longitudes = [loc.lon for loc in locations]

    centroid_lat = sum(latitudes) / len(locations)
    centroid_lon = sum(longitudes) / len(locations)

    return {"lat": centroid_lat, "lon": centroid_lon}


@app.post("/get-proximity-recommendations", summary="Get Recommendations Based on Proximity")
async def get_proximity_recommendations(request: ProximityRecommendationRequest):
    """
    Generate recommendations based on the centroid of provided user locations.
    """
    centroid = calculate_centroid(request.locations)
    try:
        all_recommendations = []
        for interest in request.interests:
            results = await apple_maps_service.search(interest, centroid['lat'], centroid['lon'])

            all_recommendations.extend(results)

        # Sort recommendations by distance from centroid (if needed)
        sorted_recommendations = sorted(
            all_recommendations,
            key=lambda x: ((x['location']['lat'] - centroid['lat'])**2 +
                           (x['location']['lon'] - centroid['lon'])**2)**0.5
        )

        # Limit to top N recommendations if needed
        top_recommendations = sorted_recommendations[:20]

        return {"recommendations": sorted_recommendations}
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Error getting recommendations: {str(e)}")

# uvicorn app:app --reload

if (os.getenv('DEV')):
    if __name__ == "__main__":
        # For development use only
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
else:
    # Production use
    if __name__ == "__main__":
        update_apple_token()
        import uvicorn
        uvicorn.run(app, host="0.0.0.0", port=8000)
