package ai.metaorcha.metabot;

/** Fixed list. The four words are the first four fingerprint bytes indexed here. */
public final class Words {
    private Words() {}

    public static final String[] LIST = {
        "apple", "river", "stone", "cloud", "maple", "cedar", "birch", "pine",
        "oak", "elm", "ash", "willow", "reed", "moss", "fern", "ivy",
        "vine", "grass", "leaf", "bark", "twig", "root", "seed", "bloom",
        "petal", "fruit", "berry", "grain", "wheat", "corn", "rice", "oat",
        "horse", "sheep", "goat", "cow", "bird", "duck", "goose", "swan",
        "dove", "hawk", "owl", "crane", "robin", "sparrow", "eagle", "whale",
        "shark", "trout", "bass", "perch", "frog", "snake", "lizard", "bear",
        "wolf", "fox", "deer", "hare", "rabbit", "mouse", "squirrel", "otter",
        "bread", "milk", "honey", "salt", "sugar", "butter", "cheese", "egg",
        "water", "fire", "wind", "rain", "snow", "ice", "mist", "fog",
        "sun", "moon", "star", "sky", "dawn", "dusk", "light", "shade",
        "hill", "lake", "pond", "bay", "sea", "wave", "tide", "sand",
        "path", "road", "bridge", "gate", "house", "door", "room", "table",
        "chair", "lamp", "clock", "book", "pen", "paper", "ink", "page",
        "happy", "quiet", "brave", "calm", "kind", "bold", "swift", "clear",
        "north", "south", "east", "west", "spring", "summer", "autumn", "winter",
        "morning", "night", "field", "garden", "orchard", "meadow", "forest", "mother",
        "father", "sister", "brother", "friend", "child", "family", "home", "music",
        "song", "dance", "story", "poem", "letter", "sound", "color", "circle",
        "square", "round", "small", "large", "short", "young", "early", "black",
        "white", "brown", "yellow", "orange", "purple", "pink", "grey", "coffee",
        "tea", "juice", "soup", "pizza", "pasta", "salad", "lemon", "mango",
        "peach", "plum", "grape", "melon", "olive", "pepper", "ginger", "garlic",
        "onion", "carrot", "potato", "tomato", "noodle", "roasted", "baked", "boiled",
        "fresh", "crisp", "sweet", "sour", "bitter", "spicy", "mild", "warm",
        "cool", "soft", "hard", "rough", "smooth", "shiny", "dull", "heavy",
        "lightest", "tiny", "huge", "quick", "slow", "still", "bright", "dim",
        "loud", "softest", "near", "far", "inner", "outer", "upper", "lower",
        "first", "second", "third", "last", "true", "false", "open", "close",
        "start", "stop", "left", "right", "above", "below", "today", "tomorrow",
        "always", "never", "maybe", "please", "thanks", "hello", "world", "people",
        "place", "thing", "point", "line", "shape", "metal", "wood", "glass"
    };

    public static String[] four(String fingerprint) {
        byte[] raw = Fingerprint.decodeHex(fingerprint);
        if (raw == null || raw.length < 4) return null;
        String[] out = new String[4];
        for (int i = 0; i < 4; i++) out[i] = LIST[raw[i] & 0xff];
        return out;
    }
}
