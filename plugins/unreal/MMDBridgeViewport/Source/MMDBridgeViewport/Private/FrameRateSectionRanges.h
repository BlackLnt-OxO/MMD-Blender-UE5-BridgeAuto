#pragma once
#include "MovieScene.h"
#include "MovieSceneSection.h"
#include "MovieSceneSequence.h"
#include "UObject/UnrealType.h"

inline int32 ProtectFrameRateSectionRanges(UMovieSceneSequence* Sequence)
{
    UMovieScene* Scene = Sequence ? Sequence->GetMovieScene() : nullptr;
    if (!IsInGameThread() || !Scene || !Scene->GetPlaybackRange().HasLowerBound()) return -1;
    FBoolProperty* Property = FindFProperty<FBoolProperty>(UMovieSceneSection::StaticClass(), TEXT("bSupportsInfiniteRange"));
    if (!Property) return -1;
    const FFrameNumber Start = Scene->GetPlaybackRange().GetLowerBoundValue();
    int32 Count = 0;
    for (UMovieSceneSection* Section : Scene->GetAllSections())
    {
        const auto Range = Section->GetRange();
        if (!Range.HasLowerBound() || !Range.HasUpperBound() || Range.GetLowerBoundValue() <= Start ||
            !Section->GetSupportsInfiniteRange()) continue;
        Section->Modify();
        Property->SetPropertyValue_InContainer(Section, false);
        Section->MarkAsChanged();
        ++Count;
    }
    return Count;
}
