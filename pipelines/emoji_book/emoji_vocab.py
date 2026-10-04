"""Curated emoji vocabulary: ~300 commonly used emoji with a short concrete name each.

The name (not the glyph) is what OpenJev reads: hypothesis = TEMPLATE.format(name=name).
Ordered roughly by general popularity within each group; extended with story-relevant objects/animals.
"""

# (emoji, name)
EMOJI = [
    # faces & feelings
    ("😂", "laughing out loud"), ("😊", "happy smiling"), ("😍", "adoration"), ("😭", "crying"), ("😢", "sadness"),
    ("😡", "anger"), ("😱", "fear and screaming"), ("😨", "being afraid"), ("😳", "embarrassment"), ("😮", "surprise"),
    ("🤔", "thinking and wondering"), ("🙄", "annoyance and eye-rolling"), ("😴", "sleepiness"), ("🥱", "boredom and yawning"),
    ("😎", "confidence"), ("🤗", "a warm welcome"), ("😅", "nervous relief"), ("😬", "awkwardness"), ("😇", "innocence"),
    ("😈", "mischief"), ("🥺", "pleading"), ("🤯", "mind-blowing confusion"), ("😵", "dizziness"), ("🤢", "disgust"),
    ("😋", "tasting something delicious"), ("🥳", "celebration"), ("😏", "smugness"), ("😐", "indifference"),
    ("😤", "frustration"), ("😩", "weariness"), ("🤫", "secrecy and silence"), ("🤪", "craziness"), ("🥲", "bittersweet feeling"),
    ("😌", "calm relief"), ("🤒", "illness"), ("💀", "death"), ("👻", "ghosts"), ("🤡", "foolishness"),
    # hearts & symbols
    ("❤️", "love"), ("💔", "heartbreak"), ("✨", "sparkle and magic"), ("🔥", "fire"), ("💯", "perfection"),
    ("⭐", "a star"), ("🌟", "shining brightly"), ("💥", "an explosion or crash"), ("💫", "dizzy stars"), ("💤", "sleep"),
    ("💭", "a thought"), ("💬", "speaking and conversation"), ("❓", "a question"), ("❗", "an exclamation"), ("⚠️", "danger and warning"),
    ("✅", "agreement"), ("❌", "refusal"), ("🚫", "prohibition"), ("🎵", "music and singing"), ("🎶", "a song"),
    ("🔔", "a bell ringing"), ("📣", "shouting and announcing"), ("🔒", "something locked"), ("🔑", "a key"),
    # gestures & people
    ("👍", "approval"), ("👎", "disapproval"), ("👏", "applause"), ("🙏", "pleading or thanks"), ("🤝", "an agreement between people"),
    ("👋", "greeting and farewell"), ("👉", "pointing"), ("✋", "stopping"), ("✌️", "peace"), ("💪", "strength"),
    ("👀", "looking and watching"), ("👂", "listening"), ("👃", "a nose"), ("👄", "a mouth"), ("🧠", "a brain and cleverness"),
    ("👶", "a baby"), ("👧", "a young girl"), ("👦", "a boy"), ("👨", "a man"), ("👩", "a woman"), ("👴", "an old man"),
    ("👑", "a king or queen and a crown"), ("🤴", "a king"), ("👸", "a queen or princess"), ("🧙", "a wizard or magic"),
    ("🧚", "a fairy"), ("🧛", "a vampire"), ("👮", "the law and officers"), ("👨‍⚖️", "a judge and a trial"), ("💃", "dancing"),
    ("🏃", "running"), ("🚶", "walking"), ("🧍", "standing still"), ("🏊", "swimming"), ("🤸", "tumbling"),
    ("👣", "footsteps"), ("🛌", "going to bed"), ("🥼", "a doctor"), ("👩‍🍳", "a cook"), ("🧑‍🎓", "a student and lessons"),
    ("👨‍👩‍👧", "a family"), ("👭", "sisters or friends"), ("🙋", "raising a hand to speak"), ("🤷", "not knowing"),
    # animals
    ("🐶", "a dog"), ("🐱", "a cat"), ("🐭", "a mouse"), ("🐰", "a rabbit"), ("🦊", "a fox"), ("🐻", "a bear"),
    ("🐼", "a panda"), ("🐨", "a koala"), ("🐯", "a tiger"), ("🦁", "a lion"), ("🐮", "a cow"), ("🐷", "a pig"),
    ("🐸", "a frog"), ("🐵", "a monkey"), ("🐔", "a chicken"), ("🐧", "a penguin"), ("🐦", "a bird"), ("🦆", "a duck"),
    ("🦅", "an eagle"), ("🦉", "an owl"), ("🦇", "a bat"), ("🐺", "a wolf"), ("🐗", "a boar"), ("🐴", "a horse"),
    ("🦄", "a unicorn"), ("🐝", "a bee"), ("🐛", "a caterpillar"), ("🦋", "a butterfly"), ("🐌", "a snail"),
    ("🐞", "a ladybug"), ("🐜", "an ant"), ("🕷️", "a spider"), ("🐢", "a turtle"), ("🐍", "a snake"), ("🦎", "a lizard"),
    ("🐙", "an octopus"), ("🦑", "a squid"), ("🦀", "a crab"), ("🦞", "a lobster"), ("🐠", "a fish"), ("🐬", "a dolphin"),
    ("🐳", "a whale"), ("🦈", "a shark"), ("🐊", "a crocodile"), ("🐘", "an elephant"), ("🦒", "a giraffe"), ("🐑", "a sheep"),
    ("🐐", "a goat"), ("🐿️", "a squirrel"), ("🦔", "a hedgehog"), ("🐉", "a dragon"), ("🦢", "a swan"), ("🦜", "a parrot"),
    ("🦩", "a flamingo"), ("🕊️", "a dove"), ("🐾", "paw prints"), ("🥚", "an egg"),
    # nature & weather
    ("🌞", "sunshine"), ("🌙", "the moon"), ("☁️", "clouds"), ("🌧️", "rain"), ("⛈️", "a storm"), ("❄️", "snow and cold"),
    ("🌈", "a rainbow"), ("🌊", "waves and water"), ("💧", "a drop of water"), ("🌪️", "a whirlwind"), ("🌬️", "wind"),
    ("🌹", "a rose"), ("🌸", "blossoms"), ("🌻", "a sunflower"), ("🌷", "a tulip"), ("🌲", "a forest and trees"),
    ("🌳", "a tree"), ("🍃", "leaves"), ("🍄", "a mushroom"), ("🌱", "growing plants"), ("🌵", "a desert"), ("⛰️", "a mountain"),
    ("🌋", "a volcano"), ("🏖️", "a beach"), ("🌍", "the world"), ("🕳️", "a deep hole"), ("🪨", "a rock"),
    # food & drink
    ("🍎", "an apple"), ("🍊", "an orange"), ("🍋", "a lemon"), ("🍌", "a banana"), ("🍓", "a strawberry"), ("🍇", "grapes"),
    ("🍒", "cherries"), ("🍑", "a peach"), ("🥕", "a carrot"), ("🌶️", "pepper and spice"), ("🥔", "a potato"), ("🍞", "bread"),
    ("🧀", "cheese"), ("🍖", "meat"), ("🍗", "a roast bird"), ("🍕", "pizza"), ("🍔", "a burger"), ("🍰", "cake"),
    ("🧁", "a cupcake"), ("🍪", "a biscuit or cookie"), ("🍫", "chocolate"), ("🍬", "sweets and candy"), ("🍭", "a lollipop"),
    ("🥧", "a tart or pie"), ("🍯", "honey and marmalade"), ("🧈", "butter"), ("☕", "tea or coffee"), ("🫖", "a teapot and tea party"),
    ("🍷", "wine"), ("🍺", "beer"), ("🥛", "milk"), ("🧪", "a potion or chemistry"), ("🍽️", "a meal and dinner"), ("🥄", "a spoon"),
    ("🔪", "a knife"), ("🥣", "a bowl of soup"),
    # objects
    ("📖", "a book and reading"), ("📚", "many books and study"), ("📜", "a scroll or a written document"), ("✏️", "writing"),
    ("🖊️", "a pen"), ("📝", "taking notes"), ("📰", "news"), ("✉️", "a letter"), ("📦", "a box"), ("🎁", "a gift"),
    ("🎈", "a balloon"), ("🎉", "a party"), ("🎂", "a birthday"), ("🎩", "a hat"), ("🧢", "a cap"), ("👒", "a bonnet"),
    ("👗", "a dress"), ("👠", "shoes"), ("👞", "boots"), ("🧤", "gloves"), ("🧣", "a scarf"), ("👓", "spectacles"),
    ("🕶️", "dark glasses"), ("💍", "a ring"), ("💎", "a jewel"), ("💰", "money"), ("🪙", "coins"), ("⏰", "a clock and being late"),
    ("⌚", "a watch"), ("⏳", "time passing"), ("📅", "a date"), ("🕰️", "the time"), ("🔍", "searching"), ("🔦", "a light in the dark"),
    ("🕯️", "a candle"), ("💡", "an idea"), ("🪞", "a mirror"), ("🚪", "a door"), ("🪟", "a window"), ("🛏️", "a bed"),
    ("🪑", "a chair"), ("🛋️", "a sofa"), ("🪜", "a ladder"), ("🧹", "sweeping"), ("🧺", "a basket"), ("🍼", "a baby bottle"),
    ("⚔️", "swords and fighting"), ("🗡️", "a dagger"), ("🏹", "a bow and arrow"), ("🔨", "a hammer"), ("🪓", "an axe"),
    ("⚖️", "justice"), ("🎭", "acting and a performance"), ("🎪", "a circus"), ("🃏", "playing cards"), ("🎲", "a game of chance"),
    ("♟️", "a game of chess"), ("🏆", "winning"), ("🥇", "first prize"), ("🎯", "aiming at a target"), ("🎨", "painting and colours"),
    ("🎤", "a performance"), ("🎻", "a violin"), ("🥁", "a drum"), ("🎺", "a trumpet"),
    # places & travel
    ("🏠", "a house and home"), ("🏰", "a castle"), ("🏫", "a school"), ("⛪", "a church"), ("🌆", "a city"), ("🌄", "a sunrise"),
    ("🚗", "a car"), ("🚂", "a train"), ("✈️", "flying"), ("🚀", "a rocket"), ("⛵", "a boat"), ("🚢", "a ship"),
    ("🗺️", "a map and journey"), ("🧭", "being lost or finding the way"), ("⚓", "an anchor"), ("🌉", "a bridge"), ("🏝️", "an island"),
    ("🏞️", "a landscape"), ("🛤️", "a path"), ("🚧", "an obstacle"), ("🏁", "a race"), ("⛲", "a fountain"), ("🌅", "a sunset"),
    # abstract / misc
    ("⬆️", "going up"), ("⬇️", "falling down"), ("🔄", "turning around"), ("🔁", "repetition"), ("➡️", "going forward"),
    ("⬛", "darkness"), ("⚪", "whiteness"), ("🔴", "redness"), ("🔵", "blueness"), ("🟢", "greenness"),
    ("🧩", "a puzzle"), ("🧵", "thread"), ("🪄", "a magic wand"), ("🔮", "fortune and the unknown"), ("🧿", "a charm"),
    ("🕸️", "a web"), ("🪤", "a trap"), ("⛓️", "chains and captivity"), ("📏", "measuring size"), ("⚖", "weighing"),
    ("🔬", "study and examination"), ("🤐", "keeping quiet"), ("🙈", "hiding"), ("🙉", "not wanting to hear"), ("🙊", "not speaking"),
    ("😶‍🌫️", "fog and uncertainty"), ("🫠", "melting away"), ("🫥", "disappearing"), ("🫧", "bubbles"), ("🔥", "burning"),
    # slang-heavy additions
    ("🍆", "an eggplant"), ("💦", "splashing water"), ("🥵", "feeling hot"),
]

# de-duplicate by glyph (keep first)
_seen = set()
EMOJI = [(e, n) for e, n in EMOJI if not (e in _seen or _seen.add(e))]

# --- groups for two-stage scoring -------------------------------------------------------------
# Section starts follow the comment blocks above (first glyph of each section).
_GROUP_DEFS = [
    ("😂", "feelings, emotions and facial expressions"),
    ("❤️", "love, symbols, sounds, danger and signs"),
    ("👍", "people, jobs, family, body language and gestures"),
    ("🐶", "animals and creatures"),
    ("🌞", "weather, plants, landscapes and the natural world"),
    ("🍎", "food, drink, eating and cooking"),
    ("📖", "everyday objects, clothes, books, tools, money, time and games"),
    ("🏠", "places, buildings, vehicles and travel"),
    ("⬆️", "directions, colours, magic, puzzles and abstract ideas"),
]
GROUPS = [name for _, name in _GROUP_DEFS]
_starts = [next(i for i, (e, _) in enumerate(EMOJI) if e == g) for g, _ in _GROUP_DEFS]
GROUP_OF = []
for _i in range(len(EMOJI)):
    GROUP_OF.append(max(g for g, st in enumerate(_starts) if _i >= st))

if __name__ == "__main__":
    from collections import Counter
    print(sorted(Counter(GROUP_OF).items()))
    print(len(EMOJI), "emoji")
