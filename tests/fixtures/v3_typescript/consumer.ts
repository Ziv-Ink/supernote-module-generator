import feature, {
  Color,
  OtherStroke,
  Point,
  Receipt,
  Stroke,
  getFeatureStatus,
  isFeatureAvailable,
  isSupernoteRangeError,
  isSupernoteTypeError,
  nativeObjectInfo,
} from "./index";

const point: Point = { x: 1, tags: ["ink", null], color: "RED" };
point.x = 2;
const color: Color = point.color;
const stroke = feature.Stroke.create(point);
stroke.label = color;
const id: bigint = stroke.id;
const transformed: Promise<Stroke> = stroke.transform(point);
const maybe: Stroke[] | null = feature.maybe([stroke, null]);
const receipt: Receipt = feature.load();
const status: string = receipt.status();
const version: string = feature.Tools.version();
const acceptsPoint: boolean = feature.Stroke.create.accepts(point);
const checked = feature.maybe.checkArguments([stroke, null]);
if (!checked.ok && (isSupernoteTypeError(checked.error) || isSupernoteRangeError(checked.error))) {
  const path: string = checked.error.path;
  void path;
}
const unknownValue: unknown = stroke;
if (feature.Receipt.is(unknownValue)) {
  unknownValue.status();
}
const statusValue = getFeatureStatus();
const available: boolean = isFeatureAvailable();
const info = nativeObjectInfo(stroke);
if (info) {
  const family: "cpp" | "jvm" = info.originFamily;
  void family;
}

// @ts-expect-error native fields mirror source read-only state
stroke.id = 2n;

// @ts-expect-error native objects are nominal and cannot be object literals
const fakeStroke: Stroke = { id: 1n, label: "fake", transform: async () => stroke };

// @ts-expect-error distinct native object declarations are not assignable
const wrongObject: OtherStroke = stroke;

// @ts-expect-error nullability is explicit
feature.maybe([undefined]);

// @ts-expect-error enum values are the declared string literals only
const wrongColor: Color = "GREEN";

void transformed;
void maybe;
void id;
void status;
void version;
void acceptsPoint;
void statusValue;
void available;
void fakeStroke;
void wrongObject;
void wrongColor;
