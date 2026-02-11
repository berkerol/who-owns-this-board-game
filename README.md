# Who Owns This Board Game

Simple website (and python script) to search through board games and show WhatsApp Display Names and BGG usernames of the people who owns them.

* `generate_games_js.py` is running daily with GitHub Actions to generate `games.js` from `users.js`.
* `users.js` is a BGG username to WhatsApp display name map.
* `games.js` is a list with BGG game ID, game name, owners list with BGG usernames.
